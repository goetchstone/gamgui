"""A person's Security tab (back-port plan B4): Chrome profiles, phones and computers, in SwiftGamGui's words and through its
strict mock handlers. Reads only. Offline only: none has run live in either app."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import GAMCommands, filter_address
from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.person_reads import (
    CHROME_SCOPE_NOTE,
    DELEGATION_NOTE,
    SCOPE_LIST_NOTE,
    chrome_note,
    chrome_profiles_from,
    computer_note,
    computers_from,
    mobile_devices_from,
)
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.core.setup import DEVICES_SCOPE, DWD_SCOPES, SetupService
from gamgui.web.server import AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
CAROL = "carol@example.com"


@pytest.mark.parametrize("build,argv", [
    (GAMCommands.print_mobile_devices, ["print", "mobile", "query", f"email:{CAROL}", "fields",
                                        "resourceid,email,model,os,type,status,firstsync,lastsync,devicecompromisedstatus",
                                        "listlimit", "0", "formatjson"]),
    (GAMCommands.print_chrome_profiles, ["print", "chromeprofiles", "filter", f'user_email="{CAROL}"', "fields",
                                         "displayname,useremail,lastactivitytime,firstenrollmenttime,osplatformtype,osversion,"
                                         "browserversion,browserchannel,deviceinfo,affiliationstate", "formatjson"]),
    (GAMCommands.print_devices, ["print", "devices", "query", f"email:{CAROL}", "fields",
                                 "devicetype,model,manufacturer,osversion,hostname,serialnumber,ownertype,managementstate,"
                                 "compromisedstate,createtime,lastsynctime", "userfields",
                                 "name,useremail,createtime,firstsynctime,lastsynctime,managementstate", "formatjson"]),
])
def test_each_read_s_argv(build, argv):
    # SwiftGamGui's, but for the phones' `listlimit 0`: without it GAM lists a shared device's first owner only.
    assert build("Carol@Example.com") == argv      # lowercased as Google keeps it


@pytest.mark.parametrize("bad", ["", "carol", "@example.com", "carol@", "carol@example", 'a"b@example.com', "a b@example.com",
                                 "a@b@example.com", "carol@example..com", "carol@example.com\n", "a:b@example.com", "a?b@example.com"])
def test_an_address_that_would_not_stay_one_search_value_is_refused(bad):
    with pytest.raises(ValueError):
        filter_address(bad)


def test_filter_address_takes_google_s_address_characters():
    assert filter_address("O'Neil.A-b_c@Sub.Example-1.com") == "o'neil.a-b_c@sub.example-1.com"


def test_chrome_profiles_most_recently_used_first_and_in_words():
    profiles = chrome_profiles_from([
        {"displayName": "Someone else's", "userEmail": "carol@example.com.au"},
        {"displayName": "Old", "userEmail": "Carol@Example.com", "lastActivityTime": "2025-01-01T00:00:00Z", "deviceInfo": {"machine": "OptiPlex"},
         "affiliationState": "AFFILIATED_CLOUD_MANAGED"},
        {"displayName": "", "lastActivityTime": "not a date", "affiliationState": ""},
        {"displayName": "Work", "lastActivityTime": "2026-10-19T14:58:00.123Z", "firstEnrollmentTime": "2025-03-04T15:20:00Z",
         "deviceInfo": {"hostname": "CAROL-MBP", "machine": "MacBookPro"}, "osPlatformType": "MAC", "osVersion": "15.6",
         "browserVersion": "141.0", "browserChannel": "STABLE", "affiliationState": "UNAFFILIATED_CLOUD_MACHINE"},
    ], CAROL)
    assert [p.display_name for p in profiles] == ["Work", "Old", ""]
    work = profiles[0]
    assert work.where == "Work on CAROL-MBP" and work.system == "macOS 15.6" and work.browser == "141.0 (stable)"
    assert work.last_used == "2026-10-19 14:58" and work.first_seen == "2025-03-04 15:20" and work.on_managed_device is False
    assert profiles[1].on_managed_device is True and profiles[1].where == "Old on OptiPlex"
    assert profiles[2].on_managed_device is None and profiles[2].where == "Chrome profile" and profiles[2].last_used == "not a date"


def test_only_a_phone_that_lists_the_person_exactly_is_theirs():
    devices = mobile_devices_from([
        {"email": ["Carol@example.com"], "model": "Pixel 8", "os": "Android 15", "type": "ANDROID", "status": "APPROVED",
         "deviceCompromisedStatus": "No compromise detected"},
        {"email": ["carol@example.com.au"], "model": "Not hers"},
        {"email": "carol@example.com", "model": "No list"},
        {"email": [CAROL], "type": "GOOGLE_SYNC", "deviceCompromisedStatus": "Compromise detected"},
    ], CAROL)
    assert [d.words for d in devices] == ["Pixel 8, Android 15 (Android)", "A device (Exchange sync (Google Sync))"]
    assert [d.reports_compromise for d in devices] == [False, True]


def _device(**fields):
    return {"deviceType": "WINDOWS", "users": [{"userEmail": CAROL, "createTime": "2026-10-19T14:12:41Z",
                                                "firstSyncTime": "2026-10-20T00:00:00Z"}], **fields}


def test_only_a_computer_that_lists_the_person_exactly_is_theirs():
    assert computers_from([_device(deviceType="ANDROID")], CAROL) == []        # a phone is the phones list's
    assert computers_from([_device(users=[{"userEmail": "carol@example.com.au"}])], CAROL) == []
    assert computers_from([_device(users="not a list")], CAROL) == []
    assert len(computers_from([_device()], "Carol@Example.com")) == 1


def test_a_computer_in_words():
    pc = computers_from([_device(model="Virtual Machine", manufacturer="Microsoft Corporation", osVersion="Windows 10.0.19045",
                                 hostname="DESKTOP-7Q2", ownerType="BYOD", serialNumber="S1", compromisedState="COMPROMISED",
                                 managementState="APPROVED")], CAROL)[0]
    assert pc.words == "Virtual Machine, Windows 10.0.19045 (Windows), named DESKTOP-7Q2"
    assert pc.looks_virtual and pc.reports_compromise and pc.is_active and pc.state == "" and pc.ownership == "personal"
    assert pc.first_signed_in == "2026-10-19 14:12"     # their own createTime: Google moves firstSyncTime at each registration


@pytest.mark.parametrize("model,maker,virtual", [("MacBook Pro", "Apple Inc.", False), ("VMware7,1", "VMware, Inc.", True),
                                                 ("", "innotek GmbH", True), ("Standard PC", "QEMU", True), ("", "", False)])
def test_a_virtual_machine_is_known_by_model_or_maker(model, maker, virtual):
    assert computers_from([_device(model=model, manufacturer=maker)], CAROL)[0].looks_virtual is virtual


@pytest.mark.parametrize("state,words,active", [("", "", True), ("APPROVED", "", True), ("BLOCKED", "blocked", False),
                                                ("PENDING_APPROVAL", "waiting for approval", True), ("WIPED", "the account wiped", False),
                                                ("UNENROLLED", "unenrolled", False), ("SOMETHING_NEW", "something_new", True)])
def test_the_person_s_state_on_it_wins_over_the_device_s(state, words, active):
    users = [{"userEmail": CAROL, "managementState": state}]
    pc = computers_from([_device(users=users, managementState="BLOCKED" if not state else "APPROVED")], CAROL)[0]
    if state:
        assert pc.state == words and pc.is_active is active
    else:
        assert pc.state == "blocked" and not pc.is_active   # nothing of their own: the device's


def _error(exit_code, stderr, kind=GAMErrorKind.UNKNOWN):
    return GAMError(kind=kind, exit_code=exit_code, stderr=stderr)


def test_each_cause_of_a_refused_read_has_its_own_fix():
    assert chrome_note(_error(15, "no scopes")) == CHROME_SCOPE_NOTE
    assert chrome_note(_error(50, "x", GAMErrorKind.SCOPE_MISSING)) == CHROME_SCOPE_NOTE
    assert chrome_note(_error(50, "Something else")) == ""
    assert computer_note(_error(73, "not enabled")) == DELEGATION_NOTE
    assert computer_note(_error(12, "ERROR: API access Denied\n")) == SCOPE_LIST_NOTE
    # A dead key is exit 12 too, and says both: no scope fixes it.
    assert computer_note(_error(12, "Authentication Token Error\nERROR: API access Denied\n")) == ""
    assert computer_note(_error(2, "Usage")) == ""


# --- through the mock and the page --------------------------------------------------------------------------------


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


def _state(gam_state: Path, folder: str, name: str) -> None:
    (gam_state / folder).mkdir(parents=True, exist_ok=True)
    (gam_state / folder / name).write_text("")


def test_the_tab_reads_only_when_opened(client, gam_calls):
    page = client.get("/users/detail", params={"email": CAROL}).text
    assert 'id="tab-security"' in page and 'id="panel-security"' in page
    assert not [argv for argv in gam_calls() if argv[:2] in (["print", "chromeprofiles"], ["print", "mobile"], ["print", "devices"])]


def test_chrome_profiles_on_the_tab(client):
    panel = client.get("/users/security/chrome", params={"email": CAROL}).text
    assert "2 Chrome profiles signed in with this account" in panel and "Work on CAROL-MBP" in panel
    assert "Saved passwords are never shown" in panel and "a computer the organization doesn't manage" in panel
    assert panel.index("CAROL-MBP") < panel.index("OptiPlex 7010")


def test_nobody_s_chrome_says_so(client):
    assert "Google knows of no Chrome profile" in client.get("/users/security/chrome", params={"email": "bob@example.com"}).text


def test_phones_on_the_tab(client):
    panel = client.get("/users/security/mobile", params={"email": CAROL}).text
    assert "Pixel 8, Android 15 (Android)" in panel and "Exchange sync" in panel


def test_a_shared_device_is_each_owner_s(client):
    # GAM lists only a device's first owner unless told otherwise, so the second looked as if they had no phone.
    for owner in ("alice@example.com", "bob@example.com"):
        assert "Shared iPad" in client.get("/users/security/mobile", params={"email": owner}).text


@pytest.mark.hand_built_argv
def test_the_mock_cuts_an_owner_list_as_gam_does_without_listlimit(client):
    conn = client.app.state.gamgui.connector
    argv = [a for a in GAMCommands.print_mobile_devices("bob@example.com")]
    cut = argv[:argv.index("listlimit")] + argv[argv.index("listlimit") + 2:]

    out = client.portal.call(conn.runner.run_authenticated, DOMAIN, cut)
    assert "Shared iPad" in out and "bob@example.com" not in out


def test_computers_on_the_tab_mark_a_virtual_machine(client):
    panel = client.get("/users/security/computers", params={"email": CAROL}).text
    assert "Virtual Machine, Windows 10.0.19045 (Windows), named DESKTOP-7Q2LM4K" in panel and "MacBook Pro" in panel
    assert "Pixel 8" not in panel       # a phone is the phones list's
    assert panel.count(">Virtual machine<") == 1


def test_a_missing_chrome_scope_says_how_to_add_it(client, gam_state):
    _state(gam_state, "noscope", "chromemanagement")
    assert "Chrome Management API" in client.get("/users/security/chrome", params={"email": CAROL}).text
    (gam_state / "noscope" / "chromemanagement").unlink()
    _state(gam_state, "denied", "chromeprofiles")
    assert "Chrome Management API" in client.get("/users/security/chrome", params={"email": CAROL}).text


def test_a_missing_devices_delegation_says_how_to_add_it(client, gam_state):
    _state(gam_state, "noscope", "devices")
    panel = client.get("/users/security/computers", params={"email": CAROL}).text
    assert "cloud-identity.devices" in panel and "no computer" not in panel


async def test_verify_s_link_keeps_the_devices_scope(vault, runner, domain):
    # GAM's link names only what it was asked about and overwrites the client's list: following it took the scope away.
    vault.set(domain, "oauth2service", '{"client_id": "12345"}')
    result = await SetupService(vault, runner).verify(domain, "partialdwd@example.com")
    assert not result.ok and "cloud-identity.devices" in unquote(result.auth_url)


def test_a_scope_list_without_devices_says_delegating_will_not_help(client, gam_state):
    _state(gam_state, "denied", "devices")
    _state(gam_state, "noscope", "devices")     # it wins: GAM refuses before it asks Google
    assert "delegating it alone won" in client.get("/users/security/computers", params={"email": CAROL}).text.replace("&#39;", "'")


def test_a_dead_key_is_no_scope_s_fault(client, gam_state):
    _state(gam_state, "badkey", "devices")
    panel = client.get("/users/security/computers", params={"email": CAROL}).text
    assert "cloud-identity.devices" not in panel and "delegating" not in panel


def test_one_read_failing_leaves_the_others(client, gam_state):
    _state(gam_state, "denied", "mobile")
    assert "Pixel 8" not in client.get("/users/security/mobile", params={"email": CAROL}).text
    assert "CAROL-MBP" in client.get("/users/security/chrome", params={"email": CAROL}).text


def test_a_bad_address_reaches_no_gam(client, gam_calls):
    panel = client.get("/users/security/mobile", params={"email": "x:y@example.com\n"}).text
    assert "isn" in panel and not [argv for argv in gam_calls() if argv[:2] == ["print", "mobile"]]


def test_a_device_s_name_is_only_ever_text(client, monkeypatch):
    conn = client.app.state.gamgui.connector

    async def hostile(email):
        return computers_from([_device(model="<img src=x onerror=alert(1)>", hostname="<script>alert(2)</script>")], CAROL)

    monkeypatch.setattr(conn, "computers", hostile)
    panel = client.get("/users/security/computers", params={"email": CAROL}).text
    assert "<img" not in panel and "<script" not in panel and "&lt;img" in panel


def test_not_connected_says_so(tmp_path):
    vault = SecretsVault(InMemoryBackend())
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    state = AppState(vault=vault, runner=runner, audit_domain="", connector=None, token="t")
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        assert c.get("/users/security/chrome", params={"email": CAROL}).status_code in (200, 400, 422)


def test_the_devices_scope_is_offered_but_not_required(vault, runner):
    # An install delegated before it must stay connected: verify's check is DWD_SCOPES alone.
    assert DEVICES_SCOPE not in DWD_SCOPES
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    details = SetupService(vault, runner).dwd_details(DOMAIN)
    assert DEVICES_SCOPE[0] in details["scopes_csv"] and DEVICES_SCOPE[0].replace(":", "%3A").replace("/", "%2F") in details["auth_url"] \
        or DEVICES_SCOPE[0] in details["auth_url"]
    assert DEVICES_SCOPE in details["scopes"]


def test_reads_run_side_by_side_and_outside_the_write_lock(client):
    conn = client.app.state.gamgui.connector

    async def both():
        return await asyncio.gather(conn.chrome_profiles(CAROL), conn.mobile_devices(CAROL))

    profiles, phones = client.portal.call(both)
    assert profiles and phones
