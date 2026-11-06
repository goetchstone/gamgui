"""What an attacker can leave on an account beyond its mail (back-port plan E7): computers, phones, connected apps and Apps Script projects in Check Account, and blocking a computer or taking the account off it.
Ported from SwiftGamGui's CompromisedTests and CompromisedChangesTests. Offline only: neither write has run on a real tenant."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connected_apps import Reach
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import GAMCommands as C
from gamgui.core.gam.commands import is_device_user_name
from gamgui.core.gam.errors import GAMError
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.incident import reader
from gamgui.core.incident.check import Account, findings, pick_computer
from gamgui.core.incident.secure import steps
from gamgui.core.person_reads import Computer, MobileDevice, ScriptProject, script_projects_from
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import TEMPLATES, AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS, wait_for_job

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
CAROL = "carol@example.com"
VM = "devices/0vmcarol01/deviceUsers/u1"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
ON_INCIDENT = {"HX-Current-URL": "http://testserver/incident"}


# --- the writes -------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [VM, "devices/a-b_c/deviceUsers/D-1_e"])
def test_a_device_user_s_name_is_one_argv_element(name):
    assert is_device_user_name(name)
    assert C.block_device_user(name) == ["block", "deviceuser", name] and C.delete_device_user(name) == ["delete", "deviceuser", name]


@pytest.mark.parametrize("name", ["", "id", "query:email:a@b.c", "devices/-/deviceUsers/-", "devices/a/deviceUsers/-", "devices/-/deviceUsers/b", "devices//deviceUsers/b", "devices/a/deviceUsers/",
                                  "devices/a/deviceUsers/b/c", "devices/a b/deviceUsers/c", "devices/a,b/deviceUsers/c", "devices/é/deviceUsers/c", f"devices/{'a' * 257}/deviceUsers/b", "users/a/deviceUsers/b"])
def test_nothing_else_is_ever_a_device_user(name):
    assert not is_device_user_name(name)
    for builder in (C.block_device_user, C.delete_device_user):
        with pytest.raises(ValueError, match="deviceUsers"):
            builder(name)


@pytest.mark.parametrize("builder", [C.print_script_projects, C.print_user_tokens])
@pytest.mark.parametrize("address", ["a@b.c,d@e.f", "id:1", "name <a@b.c>", ""])
def test_a_person_s_reads_name_one_address(builder, address):
    with pytest.raises(ValueError, match="isn't one email address"):
        builder(address)


# --- Apps Script: exit 60 is "none" only on GAM's word -----------------------------------------------------------------------------


NONE_SAID = f"Getting all Drive Files/Folders that match query (x) for {CAROL}\n\rGot 0 Drive Files/Folders that matched query (x) for {CAROL}...\r\n"


def test_exit_60_with_gam_s_word_is_no_project():
    assert script_projects_from(60, "Owner,id,name,JSON\n", NONE_SAID, [], CAROL) == []


@pytest.mark.parametrize("stderr", ["", f"User: {CAROL}, Drive Service/App not enabled\n", f"User: {CAROL}, User Print Failed: invalid_grant\n", NONE_SAID + f"User: {CAROL}, Drive Service/App not enabled\n",
                                    "Got 0 Drive Files/Folders that matched query (x) for other@example.com...\n"])
def test_exit_60_with_anything_else_is_a_failure_never_a_false_all_clear(stderr):
    with pytest.raises(GAMError):
        script_projects_from(60, "", stderr, [], CAROL)


def test_another_exit_is_a_failure():
    with pytest.raises(GAMError):
        script_projects_from(50, "", NONE_SAID, [], CAROL)


def _project(modified: datetime, created: datetime = NOW - timedelta(days=400), name="Helper"):
    return ScriptProject("1", name, "", created, "", modified, "", False)


def test_recent_is_made_or_changed_in_two_weeks():
    assert _project(NOW - timedelta(days=13)).is_recent(NOW) and _project(NOW - timedelta(days=30), NOW - timedelta(days=1)).is_recent(NOW)
    assert not _project(NOW - timedelta(days=15)).is_recent(NOW)


# --- findings --------------------------------------------------------------------------------------------------------------------------


ACCOUNT = Account(CAROL)


def _computer(active=True, model="Virtual Machine", name=VM, device_id="devices/0vmcarol01", first=NOW, compromised=False, state=""):
    return Computer("Windows", model, "", "Windows 10", "", "", "", state, active, "", "", compromised, name, device_id, first)


def test_the_findings_say_what_was_left_on_the_account():
    from types import SimpleNamespace

    apps = [SimpleNamespace(name="Mail Sync", looks_like_ai=False, reach=Reach.BROAD), SimpleNamespace(name="Read AI", looks_like_ai=True, reach=Reach.SOME)]
    said = " ".join(findings(ACCOUNT, apps=apps, phones=[MobileDevice("Pixel", "Android", "", "", "", "", True)], computers=[_computer(), _computer(active=False, model="Mac")],
                             scripts=[_project(NOW - timedelta(days=1)), _project(NOW - timedelta(days=99), name="Old")], now=NOW))
    assert "2 apps have access to carol@example.com's account (broad: Mail Sync; an AI assistant or agent by its name: Read AI)" in said
    assert "A phone or tablet syncs" in said and "rooted or jailbroken" in said
    assert "A computer is signed in with carol@example.com's account, and it's a virtual machine" in said and "1 more is already blocked or wiped" in said
    assert "An Apps Script project in carol@example.com's Drive was made or changed in the last two weeks (“Helper”)" in said and "Old" not in said


def test_a_read_that_failed_or_found_nothing_says_nothing_here():
    assert findings(ACCOUNT, apps=[], phones=[], computers=[], scripts=[]) == findings(ACCOUNT)
    assert "signed in with" not in " ".join(findings(ACCOUNT, computers=[_computer(active=False)]))


# --- which computer a to-do item is about ---------------------------------------------------------------------------------------------


def test_a_to_do_item_s_computer_by_its_id():
    vm, other = _computer(), _computer(name="devices/o/deviceUsers/u", device_id="devices/o")
    assert pick_computer([other, vm], "0vmcarol01", None) is vm


def test_one_found_by_id_and_no_longer_signed_in_picks_nothing():
    assert pick_computer([_computer(active=False), _computer(name="devices/o/deviceUsers/u", device_id="devices/o")], "0vmcarol01", NOW) is None


def test_with_no_id_match_the_nearest_within_two_minutes():
    near, nearer = _computer(first=NOW + timedelta(seconds=100), device_id="devices/a"), _computer(first=NOW - timedelta(seconds=10), device_id="devices/b")
    assert pick_computer([near, nearer], "nosuch", NOW) is nearer and pick_computer([near], "", NOW) is near
    assert pick_computer([_computer(first=NOW + timedelta(seconds=121))], "", NOW) is None


def test_a_computer_gam_can_t_name_is_never_picked():
    assert pick_computer([_computer(name="")], "0vmcarol01", NOW) is None and pick_computer([_computer(name="")], "", NOW) is None and pick_computer([], "x", None) is None


# --- the plan ---------------------------------------------------------------------------------------------------------------------------


def test_each_computer_is_one_step_after_the_send_as_addresses():
    planned = steps(CAROL, frozenset(), send_as=["x@example.com"], auto_reply_off=True, computers=[(_computer(), "block"), (_computer(name="devices/b/deviceUsers/u"), "delete")])
    assert [s.key for s in planned] == ["send_as", "computer", "computer", "auto_reply"]
    assert planned[1].commands == [["block", "deviceuser", VM]] and planned[2].commands == [["delete", "deviceuser", "devices/b/deviceUsers/u"]]
    assert planned[1].label.startswith("Block Virtual Machine") and "approving it in the Admin console undoes it" in planned[1].summary
    assert "Take carol@example.com's account off" in planned[2].label and "revoke its access" in planned[2].summary


def test_a_computer_step_waits_for_the_password_and_only_two_things_can_be_done():
    planned = steps(CAROL, frozenset({"password"}), computers=[(_computer(), "block")])
    assert planned[-1].requires == ("password",)
    with pytest.raises(ValueError, match="blocked, or the account taken off"):
        steps(CAROL, frozenset(), computers=[(_computer(), "wipe")])
    with pytest.raises(ValueError, match="deviceUsers"):
        steps(CAROL, frozenset(), computers=[(_computer(name="devices/-/deviceUsers/-"), "block")])


# --- through the screens, on the mock -----------------------------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path, monkeypatch, gam_state):
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


def _check(client, state, person=CAROL, query="") -> str:
    client.post(f"/incident/check{query}", data={"person": person})
    wait_for_job(client, state.jobs[state.incident_check_job])
    return unescape(client.get(f"/incident/check/status?job={state.incident_check_job}", headers=ON_INCIDENT).text)


def _state_file(gam_state, *path):
    target = gam_state.joinpath(*path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("")


def test_the_check_lists_what_was_left_and_changes_nothing(client, state, gam_calls):
    page = _check(client, state)
    assert "Virtual Machine" in page and "a virtual machine" in page and "Block it" in page and "Take the account off it" in page
    assert "Inbox Helper" in page and "Mail Sync Pro" in page and "Phones and tablets" in page and "Apps Script projects" in page
    assert not [a for a in gam_calls() if a[:1] in (["block"], ["delete"])]
    assert ["user", CAROL, "print", "tokens"] in gam_calls() and C.print_script_projects(CAROL) in gam_calls() and C.print_devices(CAROL) in gam_calls()


def test_someone_with_none_says_none_not_nothing(client, state):
    page = _check(client, state, person="bob@example.com")
    assert "None standalone in their Drive" in page and "None that Google's endpoint management knows" in page and "Couldn't read" not in page


@pytest.mark.parametrize("marker,says", [(("noscope", "devices"), "domain-wide delegation"), (("denied", "devices"), "service account")])
def test_without_the_devices_scope_it_says_how_never_no_computer(client, state, gam_state, marker, says):
    _state_file(gam_state, *marker)
    page = _check(client, state)
    assert "Couldn't read the computers" in page and says in page and "None that Google's endpoint management knows" not in page
    assert "Inbox Helper" in page, "the rest of the check reads as before"


@pytest.mark.parametrize("marker", [("nodrive", CAROL), ("quiet", CAROL)])
def test_drive_off_or_a_quiet_refusal_is_never_no_project(client, state, gam_state, marker):
    _state_file(gam_state, *marker)
    page = _check(client, state)
    assert "Couldn't read the Apps Script projects" in page and "None standalone" not in page


def test_tokens_refused_is_said(client, state, gam_state):
    _state_file(gam_state, "denied", "tokens")
    assert "Couldn't read the connected apps" in _check(client, state)


def _token(html) -> str:
    return re.search(r'"preview": "([A-Za-z0-9_\-]+)"', unescape(html)).group(1)


def _preview(client, state, **picks):
    return client.post("/incident/secure/preview", data={"run": state.incident_check.run, **picks}, headers=ON_INCIDENT).text


def _run(client, state, confirm):
    client.post("/incident/secure/apply", data={"confirmed": "1", "preview": _token(confirm), "confirm_email": CAROL}, headers=ON_INCIDENT)
    wait_for_job(client, state.jobs[state.incident_run_job])


def test_block_it_is_previewed_guarded_and_run_once(client, state, gam_calls, tmp_path):
    _check(client, state)
    confirm = _preview(client, state, **{f"computer:{VM}": "block"})
    assert "Block Virtual Machine" in unescape(confirm) and "block deviceuser" in unescape(confirm)
    assert not [a for a in gam_calls() if a[:1] == ["block"]], "a preview runs nothing"
    _run(client, state, confirm)
    assert [a for a in gam_calls() if a[:1] == ["block"]] == [["block", "deviceuser", VM]]
    assert '"block_device_user"' in (tmp_path / "audit.jsonl").read_text()


def test_take_the_account_off_runs_delete_and_the_check_again_drops_it(client, state, gam_calls):
    _check(client, state)
    _run(client, state, _preview(client, state, **{f"computer:{VM}": "delete"}))
    assert ["delete", "deviceuser", VM] in gam_calls()
    assert "Virtual Machine" not in _check(client, state)


def test_a_blocked_computer_stays_listed_and_can_t_be_picked_again(client, state):
    _check(client, state)
    _run(client, state, _preview(client, state, **{f"computer:{VM}": "block"}))
    page = _check(client, state)
    assert "Virtual Machine" in page and f'name="computer:{VM}"' not in page


def test_keep_it_plans_nothing_and_a_name_the_check_didn_t_list_is_passed_over(client, state, gam_calls):
    _check(client, state)
    assert "nothing to run" in unescape(_preview(client, state, **{f"computer:{VM}": "keep"}))
    assert "nothing to run" in unescape(_preview(client, state, **{"computer:devices/other/deviceUsers/u": "block"}))
    assert "nothing to run" in unescape(_preview(client, state, **{f"computer:{VM}": "wipe"}))


def test_a_refused_block_is_said_on_the_run(client, state, gam_state):
    _state_file(gam_state, "refused", "deviceusers")
    _check(client, state)
    _run(client, state, _preview(client, state, **{f"computer:{VM}": "block"}))
    run = state.jobs[state.incident_run_job]
    assert run.failed_total == 1


# Do It Here


def test_do_it_here_links_to_that_computer():
    def link(draft):
        task = reader.Task("1", "deleteDevice", "Do it", drafts=(draft,))
        reading = reader.Reading((), (), (task,), (), 1, (), None)
        return unescape(TEMPLATES.env.get_template("_incident_reading.html").render(reading=reading, part="respond", when=str, store_problems=[], marks=[], shows_query=str, group_problems={}, most_emails=100))

    page = link(reader.Draft("undo", account=CAROL, pick="computer", resource_id="0vmcarol01", registered_at=NOW))
    assert "/incident?person=carol%40example.com&fix=computer&resource=0vmcarol01&registered=2026-10-20T12%3A00%3A00%2B00%3A00" in page
    assert "registered=" not in link(reader.Draft("undo", account=CAROL, pick="computer", resource_id="x"))


def _chosen(page: str) -> dict:
    return dict(re.findall(r'name="computer:([^"]+)" value="(\w+)" checked', page))


def test_do_it_here_picks_only_that_computer_with_the_lock_off(client, state):
    page = client.get(f"/incident?person={CAROL}&fix=computer&resource=0vmcarol01").text
    assert 'hx-post="/incident/check?pick=computer&amp;resource=0vmcarol01&amp;registered="' in page
    page = _check(client, state, query="?pick=computer&resource=0vmcarol01")
    assert _chosen(page)[VM] == "block" and "Only blocking Virtual Machine" in page and "matched by when" not in page
    assert not re.findall(r'name="switch" value="\w+"[^>]* checked', page), "the lock is a to-do item of its own"


def test_do_it_here_on_the_check_shown_reads_nothing_again(client, state, gam_calls):
    _check(client, state)
    before = len(gam_calls())
    page = client.get(f"/incident?person={CAROL}&fix=computer&resource=0vmcarol01", headers=ON_INCIDENT).text
    assert _chosen(page)[VM] == "block" and len(gam_calls()) == before


def test_do_it_here_says_so_when_nothing_matches(client, state):
    page = _check(client, state, query="?pick=computer&resource=nosuch")
    assert set(_chosen(page).values()) == {"keep"} and "couldn't be matched to one still signed in" in page


def test_a_check_by_hand_drops_the_computer_pick(client, state):
    _check(client, state, query="?pick=computer&resource=0vmcarol01")
    page = _check(client, state)
    assert set(_chosen(page).values()) == {"keep"} and "password" in re.findall(r'name="switch" value="(\w+)"[^>]* checked', page)


def test_what_the_attacker_named_a_computer_is_only_ever_text(client, state, monkeypatch):
    async def computers(email):
        return [_computer(model='"><script>x</script>')]

    monkeypatch.setattr(state.connector, "computers", computers)
    client.post("/incident/check", data={"person": CAROL})
    wait_for_job(client, state.jobs[state.incident_check_job])
    page = client.get(f"/incident/check/status?job={state.incident_check_job}", headers=ON_INCIDENT).text
    assert "<script>x" not in page


# What the review of this slice proved


@pytest.mark.parametrize("field,verb", [("block", "block"), ("delete", "delete")])
def test_a_quiet_exit_is_a_failed_step_never_a_done_one(client, state, gam_state, field, verb):
    _check(client, state)
    confirm = _preview(client, state, **{f"computer:{VM}": field})
    _state_file(gam_state, "quiet", "devices")
    _run(client, state, confirm)
    run = state.jobs[state.incident_run_job]
    assert run.failed_total == 1 and "GAM exited without saying it did this" in " ".join(f"{f.reason} {f.detail}" for f in run.failed)


def test_an_operation_still_going_is_a_failed_step_never_a_done_one(client, state, gam_state):
    _check(client, state)
    confirm = _preview(client, state, **{f"computer:{VM}": "block"})
    _state_file(gam_state, "queued", "deviceusers")
    _run(client, state, confirm)
    run = state.jobs[state.incident_run_job]
    assert run.failed_total == 1 and "hasn't finished it" in " ".join(f"{f.reason} {f.detail}" for f in run.failed)


def test_a_quiet_computers_read_is_never_none(client, state, gam_state):
    _state_file(gam_state, "quiet", "devices")
    page = _check(client, state)
    assert "Couldn't read the computers: GAM exited without an answer" in page and "None that Google's endpoint management knows" not in page


def test_the_refused_write_says_the_read_only_scope_can_t_change_them(client, state, gam_state):
    _check(client, state)
    confirm = _preview(client, state, **{f"computer:{VM}": "block"})
    _state_file(gam_state, "noscope", "devices")
    _run(client, state, confirm)
    assert ".readonly form lists computers but can't change them" in " ".join(f"{f.reason} {f.detail}" for f in state.jobs[state.incident_run_job].failed)


def _computer_at(first, active=True, device_id="devices/o"):
    return _computer(active=active, first=first, device_id=device_id, name=f"{device_id}/deviceUsers/u")


def test_a_prefixed_id_is_the_same_id():
    vm = _computer()
    assert pick_computer([_computer_at(NOW), vm], "devices/0vmcarol01", None) is vm


def test_a_phone_s_to_do_never_offers_a_computer():
    from .test_incident_reader import ATTACKER, event, read

    def devices(kind):
        return read([event("login", CAROL, "login_success", {"is_suspicious": "true", "login_type": "google_password"}, ip=ATTACKER, flagged=True, minute=10),
                     event("mobile", CAROL, "DEVICE_REGISTER_UNREGISTER_EVENT", {"DEVICE_TYPE": kind, "RESOURCE_ID": "x", "ACCOUNT_STATE": "REGISTERED"}, minute=10)])

    computer = next(t for t in devices("WINDOWS").to_do if t.kind == "deleteDevice")
    assert computer.drafts and computer.drafts[0].pick == "computer" and "Do It Here" in computer.words
    phone = next(t for t in devices("IOS").to_do if t.kind == "deleteDevice")
    assert not phone.drafts and "Do It Here" not in phone.words and "Admin console" in phone.words


def test_first_signed_in_is_never_a_time_google_moves():
    from gamgui.core.person_reads import computers_from

    record = {"deviceType": "WINDOWS", "name": "devices/a", "users": [{"userEmail": CAROL, "name": "devices/a/deviceUsers/u", "firstSyncTime": "2026-10-20T12:00:00Z", "managementState": "APPROVED"}]}
    held = computers_from([record], CAROL)[0]
    assert held.first_signed_at is None and held.first_signed_in, "still shown, never matched on"


def test_a_row_still_signed_in_wins_over_one_wiped():
    from gamgui.core.person_reads import computers_from

    record = {"deviceType": "WINDOWS", "name": "devices/a", "users": [{"userEmail": CAROL, "name": "devices/a/deviceUsers/old", "managementState": "WIPED"},
                                                                      {"userEmail": CAROL.upper(), "name": "devices/a/deviceUsers/new", "managementState": "APPROVED"}]}
    held = computers_from([record], CAROL)[0]
    assert held.is_active and held.device_user == "devices/a/deviceUsers/new"


def test_a_time_match_says_it_s_a_guess(client, state):
    page = _check(client, state, query="?pick=computer&resource=nosuch&registered=2026-10-19T14:12:50Z")
    assert _chosen(page)[VM] == "block" and "matched by when it first signed in, so check it's the right one" in page


def test_computers_that_couldn_t_be_read_aren_t_called_not_found(client, state, gam_state):
    _state_file(gam_state, "noscope", "devices")
    page = _check(client, state, query="?pick=computer&resource=0vmcarol01")
    assert "The computers couldn't be read, so the to-do item's can't be picked" in page and "couldn't be matched" not in page


def test_a_run_without_the_lock_says_so(client, state):
    _check(client, state)
    _run(client, state, _preview(client, state, **{f"computer:{VM}": "block"}))
    assert not state.incident_lock_ran
    page = client.get(f"/incident/secure/status?job={state.incident_run_job}", headers=ON_INCIDENT).text
    assert "The lock wasn" in page
