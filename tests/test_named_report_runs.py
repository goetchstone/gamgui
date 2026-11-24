"""Named reports run on Audit runs (plan F7a, second half): each F2 named report is a one-step run of its catalog read built
bare, so it can be scheduled, kept and compared like an audit run. Recorded as a `read` of origin `named_report` whose
`command` is the report's key. Each read's failing, silent and partial exits, read from GAM's source at the 7.52.04 tag,
are unknown, never "none" (invariant 10), and a change a silent skip can fake says so beside it.
"""

from __future__ import annotations

from html import unescape

import pytest

from gamgui.core import audit_runs as runs
from gamgui.core import schedules as sched
from gamgui.core.catalog.catalog import load_catalog
from gamgui.core.connectors.base import RiskLevel
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.reports import NAMED_REPORTS, named_report_command

from .helpers import gam_writes
from .test_audit_runs import _records, _start
from .test_users_web import client  # noqa: F401  (the fixture)

CATALOG = load_catalog()
KEYS = [r.key for r in NAMED_REPORTS]


def _schedule(client, run, cadence="weekly", keep="3"):  # noqa: F811
    return unescape(client.post("/reports/runs/schedule", data={"run": run, "cadence": cadence, "keep": keep}).text)


def _result(client, key):  # noqa: F811
    return client.app.state.gamgui.audit_runs[key]


# --- what runs -------------------------------------------------------------------------------------------------------

def test_every_named_report_was_studied_and_runs_here_as_its_bare_catalog_read():
    assert set(runs.NAMED_READS) == set(KEYS)        # a new named report isn't offered until its read is studied
    named = runs.named_runs(CATALOG)
    assert [r.key for r in named] == KEYS
    for run in named:
        cmd = named_report_command(CATALOG, next(r for r in NAMED_REPORTS if r.key == run.key))
        [step] = run.steps
        assert run.named == run.key and run.cid == cmd.id and not cmd.sensitive and cmd.risk == RiskLevel.READ_ONLY
        assert list(step.argv) == cmd.build({s.key: "" for s in cmd.slots}) and step.argv[0] == "print"
        assert runs.step_command(run, step) == run.key and runs.step_argv(step) == list(step.argv)


def test_a_report_that_isn_t_a_plain_read_is_never_offered(monkeypatch):
    report = NAMED_REPORTS[0]
    cmd = named_report_command(CATALOG, report)
    monkeypatch.setattr(cmd, "sensitive", True)
    assert runs.named_run(report, cmd) is None
    monkeypatch.setattr(cmd, "sensitive", False)
    assert runs.named_run(report, None) is None and runs.named_run(report, cmd) is not None
    monkeypatch.delitem(runs.NAMED_READS, report.key)
    assert runs.named_run(report, cmd) is None


@pytest.mark.parametrize("key", KEYS)
def test_each_named_report_runs_reads_once_and_leaves_one_named_report_record(client, gam_calls, key):  # noqa: F811
    before = len(gam_calls())
    _, job = _start(client, key)
    run = next(r for r in runs.named_runs(CATALOG) if r.key == key)
    assert [c[:len(run.steps[0].argv)] for c in gam_calls()[before:]] == [list(run.steps[0].argv)]
    [record] = _records(client)
    assert record["ok"] and record["argv"] == list(run.steps[0].argv) and record["target"] == ""
    assert record["extra"]["origin"] == "named_report" and record["extra"]["command"] == key
    assert record["extra"]["domain"] == "example.com"
    assert record["extra"]["rows"] > 0
    result = _result(client, key)
    [table] = result.findings
    assert result.answered and table.known and len(table.rows) == record["extra"]["rows"]
    assert (run.steps[0].key_column in table.columns or not table.rows) and job.title == f"Named report: {run.title}"
    assert gam_writes(gam_calls()) == []


def test_the_page_offers_each_named_report_with_its_gam_line_and_the_builder(client):  # noqa: F811
    page = unescape(client.get("/reports/runs").text)
    assert "Named reports" in page
    for run in runs.named_runs(CATALOG):
        panel = page.split(f'id="run-{run.key}"')[1].split("</section>")[0]
        assert f"gam {' '.join(run.steps[0].argv)}" in panel and f"/builder?cid={run.cid}" in panel
    reports = unescape(client.get("/reports").text)
    assert reports.count("Run or schedule it") == len(KEYS) and 'href="/reports/runs#run-group_members"' in reports


def test_the_table_s_csv_is_the_whole_read(client):  # noqa: F811
    _start(client, "org_units")
    r = client.get("/reports/runs/table.csv?run=org_units&finding=table")
    assert r.status_code == 200 and r.headers["content-disposition"] == 'attachment; filename="org-units-table.csv"'
    assert r.text.splitlines()[0] == "orgUnitPath,orgUnitId,name,parentOrgUnitId" and len(r.text.splitlines()) == 5


# --- unknown, never none (invariant 10) -------------------------------------------------------------------------------

# (key, state, why the step is unknown, whether GAM's read itself succeeded: a table that came back but can't be an answer
# is recorded as it was, rows 0, while the step is unknown)
@pytest.mark.parametrize("key, state, words, recorded_ok", [
    ("licenses", "nolicensing", "A required API scope is not authorized", False),
    ("licenses", "licenses_silent", "GAM listed no license seat at all", True),                  # every product passed over
    ("shared_drives", "drive_token_silent", "didn't say there were none", False),                 # titles alone, exit 0
    ("shared_drive_acls", "drive_token_silent", "didn't say there were none", False),
    ("shared_drive_acls", "nodrives", "says that too when Google refused every drive", True),     # 60: none, or all refused
    ("shared_drive_organizers", "drive_token_silent", "didn't say there were none", False),
    ("shared_drive_organizers", "nodrives", "says that too when Google refused every drive", True),
    ("org_units", "orgs_failed", "", False),                                                       # 50, no CSV
    ("mobile_devices", "denied/mobile", "", False),                                                # 12, no CSV
    ("alerts", "alerts_token_silent", "GAM ended without writing the table", False),               # nothing at all, exit 0
    ("alerts", "denied/alerts", "", False),                                                        # 73, no CSV
    ("data_transfers", "denied/datatransfers", "", False),                                         # 50, no CSV
    ("data_transfers", "denied/transferapps", "", False),                                          # 61, no CSV
], ids=lambda v: v if isinstance(v, str) and "/" not in v and " " not in v else None)
def test_a_failed_or_silent_read_is_unknown_never_none(client, gam_state, key, state, words, recorded_ok):  # noqa: F811
    (gam_state / state).parent.mkdir(parents=True, exist_ok=True)
    (gam_state / state).touch()
    _, job = _start(client, key)
    result = _result(client, key)
    [table] = result.findings
    assert job.failed_total == 1 and not result.answered and not table.known and table.rows == []
    assert words in result.reads[0].why
    record = _records(client)[-1]
    assert record["extra"]["origin"] == "named_report" and record["ok"] is recorded_ok
    assert record["extra"].get("rows") == (0 if recorded_ok else None)
    assert "0 rows." not in unescape(client.get("/reports/runs").text).split(f'id="run-{key}"')[1].split("</section>")[0]


@pytest.mark.parametrize("key, state", [("shared_drives", "nodrives"), ("org_units", "no_org_units"),
                                        ("mobile_devices", "no_mobile"), ("alerts", "no_alerts"),
                                        ("data_transfers", "no_transfers")])
def test_none_is_an_answer_where_gam_says_none_its_own_way(client, gam_state, key, state):  # noqa: F811
    (gam_state / state).touch()
    _start(client, key)
    result = _result(client, key)
    assert result.answered and result.findings[0].known and result.findings[0].rows == []
    assert _records(client)[-1]["extra"]["rows"] == 0


def test_a_group_whose_members_google_refused_is_named_and_the_table_is_partial(client, gam_state):  # noqa: F811
    (gam_state / "members_gone").mkdir()
    (gam_state / "members_gone" / "it@example.com").touch()
    _start(client, "group_members")
    result = _result(client, "group_members")
    [table] = result.findings
    assert result.reads[0].state == runs.PARTIAL and result.reads[0].unread == ("it@example.com",)
    assert table.known and table.partial and "it@example.com" in table.note
    assert "it@example.com" not in {r[0] for r in table.rows} and "sales@example.com" in {r[0] for r in table.rows}
    assert _records(client)[-1]["extra"]["partial"] is True


# --- scheduled ------------------------------------------------------------------------------------------------------------

def test_a_scheduled_named_report_is_due_on_home_kept_and_says_what_changed(client, gam_state):  # noqa: F811
    _schedule(client, "alerts")
    home = unescape(client.get("/").text)
    assert 'href="/reports/runs#run-alerts"' in home and ">Google's alerts is due</a>" in home
    _start(client, "alerts")
    kept = client.app.state.gamgui.kept_runs
    assert len(kept.runs("example.com", "alerts")) == 1 and "Nothing is due." in unescape(client.get("/").text)
    (gam_state / "no_alerts").touch()                 # both alerts gone from Google's list
    _start(client, "alerts")
    change = _result(client, "alerts").changes["table"]
    assert change.compared and change.new == [] and [r[0] for r in change.gone] == ["a1b2c3d4-0001", "a1b2c3d4-0002"]
    panel = unescape(client.get("/reports/runs").text).split('id="run-alerts"')[1].split("</section>")[0]
    assert ": 0 new, 2 gone." in panel and "an alert gone may have aged out" in panel


def test_a_switch_shows_the_other_domain_s_named_schedules_and_drops_the_held_run(client):  # noqa: F811
    # F6 and F4 merged first: a switch drops a named report's last run as it does an audit run's, and shows that domain's
    # schedule and kept runs; its panel never offers a Sheet export (that is the Builder's, behind its own preview).
    st = client.app.state.gamgui
    _schedule(client, "alerts")
    _start(client, "alerts")
    panel = unescape(client.get("/reports/runs").text).split('id="run-alerts"')[1].split("</section>")[0]
    assert "Kept on this Mac" in panel and "/builder/sheet/" not in panel and "todrive" not in panel
    home_com = st.connector
    st.activate(GAMConnector(runner=st.runner, domain="example.org", audit=home_com.audit))
    assert "alerts" not in st.audit_runs and "alerts" not in st.audit_run_jobs
    panel = unescape(client.get("/reports/runs").text).split('id="run-alerts"')[1].split("</section>")[0]
    assert "Schedule: not scheduled</" in panel.replace("<span", "</") and "Kept on this Mac" not in panel
    assert "Google's alerts is due" not in unescape(client.get("/").text)
    st.activate(home_com)
    panel = unescape(client.get("/reports/runs").text).split('id="run-alerts"')[1].split("</section>")[0]
    assert "Schedule: weekly · 1 kept on this Mac" in panel


def test_a_partial_group_read_is_never_compared(client, gam_state):  # noqa: F811
    _schedule(client, "group_members")
    _start(client, "group_members")
    (gam_state / "members_gone").mkdir()
    (gam_state / "members_gone" / "it@example.com").touch()
    _start(client, "group_members")
    assert _result(client, "group_members").changes["table"].why == sched.PART_UNKNOWN_NOW


def test_a_dropped_drive_says_beside_its_change_that_it_may_be_unread(client, gam_state):  # noqa: F811
    _schedule(client, "shared_drive_acls")
    _start(client, "shared_drive_acls")
    (gam_state / "acl_dropped").mkdir()
    (gam_state / "acl_dropped" / "0ALEGALuk9PVA").touch()
    _start(client, "shared_drive_acls")
    change = _result(client, "shared_drive_acls").changes["table"]
    assert change.compared and [r[2] for r in change.gone] == ["Legal hold"]
    panel = unescape(client.get("/reports/runs").text).split('id="run-shared_drive_acls"')[1].split("</section>")[0]
    assert "a drive gone since then may be one it couldn't read this time" in panel


def test_the_bare_reads_columns_are_gam_s(client):  # noqa: F811
    for key, wanted in (("mobile_devices", ("resourceId", "email", "status")),           # email: a list joined, not email.0
                        ("shared_drives", ("User", "id", "name", "createdTime", "hidden", "orgUnit", "orgUnitId")),
                        ("shared_drive_acls", ("User", "id", "name", "createdTime", "permissions",   # the list's length
                                               "permissions.0.emailAddress"))):
        _start(client, key)
        table = _result(client, key).findings[0]
        assert table.columns[:len(wanted)] == wanted, key
    drives = {r[2]: r for r in _result(client, "shared_drives").findings[0].rows}
    assert drives["Finance"][5] == "/Sales"                     # its orgUnitId, read as a path from the org units
    acls = {r[2]: r for r in _result(client, "shared_drive_acls").findings[0].rows}
    assert acls["Finance"][4] == "2" and acls["Legal hold"][4] == "1"


def test_shared_drives_need_the_org_units_and_a_failed_lookup_is_unknown(client, gam_state):  # noqa: F811
    (gam_state / "orgs_failed").touch()                # getOrgUnitIdToPathMap iterates the None _getOrgUnits returned
    _, job = _start(client, "shared_drives")
    assert job.failed_total == 1 and not _result(client, "shared_drives").findings[0].known


def test_organizers_is_one_per_drive_so_a_second_one_never_shows_and_the_panel_says_so(client, tmp_path, monkeypatch):  # noqa: F811
    import shutil
    from pathlib import Path

    fixtures = tmp_path / "fixtures"
    shutil.copytree(Path(__file__).parent / "fixtures", fixtures)
    with open(fixtures / "shared_drive_acls.tsv", "a", encoding="utf-8") as f:     # Finance gets a second organizer
        f.write('0AFINANCEuk9PVA\t{"deleted": false, "domain": "example.com", "emailAddress": "carol@example.com", '
                '"id": "100000000000000000003", "kind": "drive#permission", "role": "organizer", "type": "user"}\n')
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(fixtures))
    _start(client, "shared_drive_organizers")
    rows = {r[1]: r[2] for r in _result(client, "shared_drive_organizers").findings[0].rows}
    assert rows["Finance"] == "alice@example.com"            # oneOrganizer: the first, never "alice… carol…"
    panel = unescape(client.get("/reports/runs").text).split('id="run-shared_drive_organizers"')[1].split("</section>")[0]
    assert "GAM lists one organizer per drive" in panel and "The Shared drive permissions audit run, above, lists every member." in panel


def test_columns_a_read_grows_or_sheds_between_runs_compare_on_what_rows_share(tmp_path):
    kept = sched.KeptRuns(tmp_path / "kept")
    cols1 = ("id", "name", "permissions.0.emailAddress")
    then = runs.Finding("table", "Shared drive permissions", "", cols1, [("A", "Team", "a@x.com"), ("B", "Ops", "b@x.com")])
    kept.keep("example.com", "shared_drive_acls", 1.0, 0, [then])
    cols2 = cols1 + ("permissions.1.emailAddress",)            # a drive gained a second member: a column more
    now = runs.Finding("table", "Shared drive permissions", "", cols2, [("A", "Team", "a@x.com", ""), ("B", "Ops", "b@x.com", "c@x.com")])
    change = sched.changes_since([now], 0, kept.runs("example.com", "shared_drive_acls"),
                                 lambda i: kept.load("example.com", "shared_drive_acls", i))["table"]
    assert change.compared and change.new == [("B", "Ops", "b@x.com", "c@x.com")] and change.gone == [("B", "Ops", "b@x.com", "")]
    assert change.columns == ()                                # now's columns hold both: drawn under the table's own
    shed = runs.Finding("table", "Shared drive permissions", "", cols1[:2], [("A", "Team")])
    change = sched.changes_since([shed], 0, kept.runs("example.com", "shared_drive_acls"),
                                 lambda i: kept.load("example.com", "shared_drive_acls", i))["table"]
    assert change.columns == cols1 and change.gone == [("A", "Team", "a@x.com"), ("B", "Ops", "b@x.com")]


# --- after the review of #178 ---------------------------------------------------------------------------------------------

def _fixtures(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    fixtures = tmp_path / "fixtures"
    shutil.copytree(Path(__file__).parent / "fixtures", fixtures)
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(fixtures))
    return fixtures


def _panel(client, key):  # noqa: F811
    return unescape(client.get("/reports/runs").text).split(f'id="run-{key}"')[1].split("</section>")[0]


def test_a_device_that_only_synced_is_no_change_but_one_blocked_is(client, tmp_path, monkeypatch):  # noqa: F811
    # lastSync moves each time a phone syncs: compared whole, every device read as gone and new (5 of 5).
    fixtures = _fixtures(tmp_path, monkeypatch)
    _schedule(client, "mobile_devices")
    _start(client, "mobile_devices")
    devices = fixtures / "mobile_devices.json"
    devices.write_text(devices.read_text().replace('"lastSync": "2026-10-', '"lastSync": "2026-11-'))
    _start(client, "mobile_devices")
    change = _result(client, "mobile_devices").changes["table"]
    assert change.compared and change.new == [] and change.gone == [] and change.left_out == ("lastSync",)
    panel = _panel(client, "mobile_devices")
    assert "No change since" in panel and "Compared without lastSync: it moves each time a device syncs" in panel
    devices.write_text(devices.read_text().replace('"status": "APPROVED"', '"status": "BLOCKED"', 1))
    _start(client, "mobile_devices")
    change = _result(client, "mobile_devices").changes["table"]
    assert len(change.new) == 1 and len(change.gone) == 1 and "BLOCKED" in change.new[0]
    assert "lastSync" not in change.columns and change.columns     # drawn in the columns compared


def test_an_alert_only_edited_is_no_change_but_one_closed_is(client, gam_state):  # noqa: F811
    _schedule(client, "alerts")
    _start(client, "alerts")
    (gam_state / "alerts_edited").touch()                 # updateTime and metadata.updateTime moved, nothing else
    _start(client, "alerts")
    change = _result(client, "alerts").changes["table"]
    assert change.compared and change.new == [] and change.gone == []
    assert change.left_out == ("metadata.updateTime", "updateTime")
    assert "A change to its status, assignee or severity still shows." in _panel(client, "alerts")
    (gam_state / "alerts_closed").touch()
    _start(client, "alerts")
    change = _result(client, "alerts").changes["table"]
    assert [r[0] for r in change.new] == ["a1b2c3d4-0001"] and "CLOSED" in change.new[0]


def test_a_schedule_its_report_can_t_run_any_more_is_never_due_and_can_be_cleared(client, monkeypatch):  # noqa: F811
    # A GAM bump that renames a named report's read: its schedule was "due" forever, and neither unscheduling nor deleting
    # its kept runs was allowed ("That isn't one of the audit runs.").
    st = client.app.state.gamgui
    _schedule(client, "alerts")
    _start(client, "alerts")
    real = runs.named_report_command
    monkeypatch.setattr(runs, "named_report_command", lambda cat, r: None if r.key == "alerts" else real(cat, r))
    home = unescape(client.get("/").text)
    assert "Google's alerts is due" not in home and "Google's alerts can't run here any more" in home
    assert 'href="/reports/runs#stranded-alerts"' in home and "Nothing is due." in home
    page = unescape(client.get("/reports/runs").text)
    assert 'id="run-alerts"' not in page and 'id="stranded-alerts"' in page and "Can't run here any more" in page
    assert "still scheduled weekly, with 1 run kept on this Mac" in page.split('id="stranded-alerts"')[1]
    refused = _schedule(client, "alerts", cadence="daily")
    assert "can't be scheduled" in refused and st.schedules.schedules("example.com")["alerts"].cadence == "weekly"
    off = _schedule(client, "alerts", cadence="")
    assert "Not scheduled. 1 kept run was deleted from this Mac." in off and "nothing of it is kept" in off
    assert "alerts" not in st.schedules.schedules("example.com") and st.kept_runs.runs("example.com", "alerts") == []
    assert "can't run here any more" not in unescape(client.get("/").text)


def test_runs_kept_for_a_run_that_no_longer_exists_are_listed_and_can_be_deleted(client):  # noqa: F811
    st = client.app.state.gamgui
    sched_store, kept = st.schedules, st.kept_runs
    assert sched_store is not None and kept is not None
    kept.keep("example.com", "old_report", 1.0, 0, [runs.Finding("table", "Old", "", ("id",), [("1",)])])
    page = unescape(client.get("/reports/runs").text).split('id="stranded-old_report"')[1].split("</section>")[0]
    assert "1 run is still kept on this Mac" in page and "Delete the kept runs" in page
    done = unescape(client.post("/reports/runs/kept/delete", data={"run": "old_report"}).text)
    assert "Deleted 1 kept run from this Mac." in done and "nothing of it is kept" in done
    assert kept.runs("example.com", "old_report") == []
    for bad in ("nothing_kept", "../old_report", "old_report\n", "Alerts"):
        assert "That isn't one of the audit runs." in unescape(client.post("/reports/runs/kept/delete", data={"run": bad}).text)
        assert "That isn't one of the audit runs." in _schedule(client, bad, cadence="")


def test_no_two_runs_share_a_title_and_the_pointers_name_the_audit_run(client):  # noqa: F811
    import re
    page = unescape(client.get("/reports/runs").text)
    titles = re.findall(r'<h2 id="run-[a-z_]+-title"[^>]*>([^<]+)</h2>', page)
    assert len(titles) == len(set(titles)) == len(runs.RUNS) + len(runs.named_runs(CATALOG))
    assert runs.SHARED_DRIVES.title == "Shared drive permissions"
    acls = next(r for r in runs.named_runs(CATALOG) if r.key == "shared_drive_acls")
    assert acls.title == "Shared drive members" and "Shared drive permissions audit run, above" in acls.change_note


def test_a_kept_run_of_a_drive_with_thousands_of_members_reads_back_whole(tmp_path):
    # Bare `print shareddriveacls` writes about 13 columns a member: 3,000 members once made the first line too long to
    # read back, so every such kept run read as broken.
    kept = sched.KeptRuns(tmp_path / "kept")
    cols = ("id", "name") + tuple(f"permissions.{i}.{f}" for i in range(3000) for f in (
        "allowFileDiscovery", "deleted", "displayName", "domain", "emailAddress", "expirationTime", "id",
        "pendingOwner", "permissionDetails", "photoLink", "role", "type", "view"))
    row = ("0Aone", "Everyone") + tuple("x" for _ in cols[2:])
    big = runs.Finding("table", "Shared drive members", "", cols, [row])
    kept.keep("example.com", "shared_drive_acls", 1.0, 0, [big])
    [head] = kept.runs("example.com", "shared_drive_acls")
    assert not head.problem and head.table("table").kept
    loaded = kept.load("example.com", "shared_drive_acls", head.id)
    assert not loaded.problem and loaded.table("table").columns == cols and loaded.table("table").rows == [row]
    change = sched.changes_since([big], 0, kept.runs("example.com", "shared_drive_acls"),
                                 lambda i: kept.load("example.com", "shared_drive_acls", i))["table"]
    assert change.compared and change.new == [] and change.gone == []


def test_a_run_kept_before_the_columns_moved_still_reads(tmp_path):
    # Kept runs written by version 1 (columns in the first line) are still read and compared with.
    import json
    kept = sched.KeptRuns(tmp_path / "kept")
    kept.keep("example.com", "org_units", 1.0, 0, [runs.Finding("table", "Org units", "", ("orgUnitPath",), [("/A",)])])
    [path] = list((tmp_path / "kept").rglob("*.jsonl"))
    head, rows = path.read_text().splitlines()
    head_d, rows_d = json.loads(head), json.loads(rows)
    head_d["version"] = 1
    head_d["tables"][0]["columns"] = rows_d.pop("columns")
    path.write_text(json.dumps(head_d) + "\n" + json.dumps(rows_d) + "\n")
    [old] = kept.runs("example.com", "org_units")
    assert not old.problem and old.table("table").columns == ("orgUnitPath",)
    assert kept.load("example.com", "org_units", old.id).table("table").rows == [("/A",)]
