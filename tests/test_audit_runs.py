"""Audit runs (plan F3): a fixed list of reads, Stop between steps, each step's table kept, bounded, and every step leaving
F1's read record. A step that failed, wrote no table, read nothing where something must be, or never ran is unknown, and
so is every table that needs it: never "none" (invariant 10)."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from html import unescape

import pytest

from gamgui.core import audit_runs as runs
from gamgui.core.gam.commands import CACHE_FIELDS
from gamgui.core.gam.models import GAMUser
from gamgui.core.phishing import Incomplete

from .helpers import gam_writes, wait_for_job
from .test_users_web import client  # noqa: F401  (the fixture)

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)
ALICE, BOB, CAROL, LEFT = "alice@example.com", "bob@example.com", "carol@example.com", "left@example.com"
STANDARD, FORMER = "Google Workspace Business Standard", "Google Vault - Former Employee"


def _user(email, last="2026-01-01T00:00:00Z", made="2024-01-01T00:00:00Z", **kw):
    return GAMUser(primary_email=email, given_name=email.split("@")[0], last_login_time=last, creation_time=made, **kw)


def _seat(email, sku="1010020028", display=STANDARD, product="Google-Apps"):
    return {"userId": email, "productId": product, "productDisplay": "Google Workspace", "skuId": sku, "skuDisplay": display}


def _read(step, state=runs.READ, why=""):
    return runs.StepRead(step, state, why)


# --- 1. Inactive users ------------------------------------------------------------------------------------------------

def test_inactive_lists_the_window_s_bucket_never_signed_in_first_and_flags_new_accounts_and_admins():
    users = [_user("old@x.com", last="2026-03-01T00:00:00Z"),
             _user("never@x.com", last="Never", made="2020-01-01T00:00:00Z"),        # GAM's word for Google's never
             _user("hire@x.com", last="Never", made="2026-11-01T00:00:00Z"),          # made inside the window
             _user("unknown@x.com", made=None),                                        # when isn't known: flagged too
             _user("boss@x.com", is_admin=True),
             _user("gone@x.com", suspended=True),                                      # suspended: not this list
             _user("busy@x.com", last="2026-11-18T00:00:00Z")]                         # signed in inside the window
    f = runs.inactive_finding(users, 90, NOW)
    assert f.known and f.columns == runs.INACTIVE_COLUMNS and f.links
    assert [r[0] for r in f.rows] == ["hire@x.com", "never@x.com", "boss@x.com", "unknown@x.com", "old@x.com"]
    flags = {r[0]: r[5] for r in f.rows}
    assert flags == {"hire@x.com": "new account", "never@x.com": "", "boss@x.com": "admin",
                     "unknown@x.com": "new account", "old@x.com": ""}
    assert {r[0]: r[2] for r in f.rows}["never@x.com"] == "never"
    assert "5 active accounts have not signed in within 90 days; 2 of them were made inside the window" in f.summary


def test_the_window_picks_the_bucket():
    users = [_user("a@x.com", last="2026-09-01T00:00:00Z")]
    assert [r[0] for r in runs.inactive_finding(users, 30, NOW).rows] == ["a@x.com"]
    assert runs.inactive_finding(users, 180, NOW).rows == []
    assert runs.inactive_finding(users, 180, NOW).summary == "Every active account has signed in within 180 days."


def test_an_unknown_directory_is_never_an_empty_inactive_list():
    for state, words in ((runs.UNKNOWN, "couldn't be read"), (runs.NOT_RUN, "stopped before")):
        [f] = runs.findings_for(runs.INACTIVE_USERS, [_read(runs.DIRECTORY, state, "Refused.")], {}, now=NOW, days=90)
        assert not f.known and not f.rows and words in f.why and "Every active account" not in f.summary


# --- 3. License usage -------------------------------------------------------------------------------------------------

SEATS = [_seat(ALICE), _seat(BOB), _seat(CAROL), _seat(LEFT),
         _seat(BOB, "Google-Vault-Former-Employee", FORMER, "Google-Vault")]
USERS = [_user(ALICE), _user(BOB, suspended=True), _user(CAROL)]


def test_seats_per_license_on_suspended_accounts_and_on_addresses_the_directory_lacks():
    reads = [_read(runs.LICENSES), _read(runs.DIRECTORY)]
    per_sku, suspended, stray = runs.license_findings(SEATS, USERS, reads)
    assert per_sku.rows == [("Google Workspace", STANDARD, "1010020028", "4", "1", "1"),
                            ("Google Workspace", FORMER, "Google-Vault-Former-Employee", "1", "1", "0")]
    assert [(r[0], r[2]) for r in suspended.rows] == [(BOB, FORMER), (BOB, STANDARD)] and suspended.links
    assert stray.rows == [(LEFT, STANDARD)] and not stray.links    # not in the directory: nobody's page to open
    assert per_sku.summary == "5 seats across 2 licenses." and "2 seats are held by a suspended account" in suspended.summary


def test_an_all_clear_covers_only_the_seats_read_and_says_so():
    # GAM passes over a product Google refuses, exit 0: a clean run may still miss one, so no all-clear stands alone.
    reads = [_read(runs.LICENSES), _read(runs.DIRECTORY)]
    per_sku, suspended, stray = runs.license_findings([_seat(ALICE)], [_user(ALICE)], reads)
    assert suspended.summary == "No suspended account holds any of the 1 seat read."
    assert stray.summary == "Every seat read is on an account the directory read listed."
    for f in (per_sku, suspended, stray):
        assert "passes over a product Google won't let it read without saying so" in f.note


def test_the_join_matches_google_s_current_primary_address_whatever_its_case():
    reads = [_read(runs.LICENSES), _read(runs.DIRECTORY)]
    _, suspended, stray = runs.license_findings([_seat("Bob@Example.com")], [_user(BOB, suspended=True)], reads)
    assert [r[0] for r in suspended.rows] == [BOB] and stray.rows == []


def test_an_unknown_directory_leaves_the_counts_and_never_says_no_suspended_seat():
    reads = [_read(runs.LICENSES), _read(runs.DIRECTORY, runs.UNKNOWN, "Refused.")]
    per_sku, suspended, stray = runs.findings_for(runs.LICENSE_USAGE, reads, {"licenses": SEATS})
    assert per_sku.known and per_sku.rows[0][3:] == ("4", "unknown", "unknown") and "last two columns are unknown" in per_sku.note
    for f in (suspended, stray):
        assert not f.known and f.rows == [] and "The directory, suspended accounts included" in f.why


def test_unknown_seats_make_every_license_table_unknown():
    reads = [_read(runs.LICENSES, runs.UNKNOWN, "Refused."), _read(runs.DIRECTORY)]
    assert all(not f.known for f in runs.findings_for(runs.LICENSE_USAGE, reads, {"directory": USERS}))


def test_nothing_where_something_must_be_is_unknown():
    assert "no account at all" in runs.implausibly_empty(runs.DIRECTORY, 0)
    assert "unknown, not none" in runs.implausibly_empty(runs.LICENSES, 0)
    assert runs.implausibly_empty(runs.LICENSES, 1) == ""


def test_each_step_is_one_print_read_from_its_builder():
    assert runs.step_argv(runs.LICENSES) == ["print", "licenses"]
    assert runs.step_argv(runs.DIRECTORY) == ["print", "users", "fields", ",".join(CACHE_FIELDS), "formatjson"]
    assert [runs.step_command(r, s) for r in runs.RUNS for s in r.steps] == [
        "inactive_users/directory", "license_usage/licenses", "license_usage/directory"]


# --- the connector's recorded read (F1) -----------------------------------------------------------------------------------

def _records(client, command=None):  # noqa: F811
    return [e for e in client.app.state.gamgui.connector.audit.tail()
            if e["action"] == "read" and (command is None or e["extra"].get("command") == command)]


def test_an_audit_read_refuses_anything_but_a_print_read_before_gam_runs(client, gam_calls):  # noqa: F811
    conn = client.app.state.gamgui.connector
    before = len(gam_calls())
    for argv in (["update", "user", ALICE, "suspended", "on"], ["user", ALICE, "print", "tokens"],
                 *(["print", "licenses", spelling] for spelling in ("todrive", "to_drive", "ToDrive", "TODRIVE"))):
        with pytest.raises(ValueError):
            client.portal.call(conn.audit_read, "x/y", argv, "userId")
    assert len(gam_calls()) == before and _records(client) == []


def test_a_read_without_its_table_raises_and_is_recorded_as_failed(client, gam_state):  # noqa: F811
    (gam_state / "licenses_silent").touch()
    conn = client.app.state.gamgui.connector
    assert client.portal.call(conn.audit_read, "license_usage/licenses", ["print", "licenses"], "userId") == []
    with pytest.raises(Incomplete):   # the titles are there, but not the column the step joins on
        client.portal.call(conn.audit_read, "license_usage/licenses", ["print", "licenses"], "primaryEmail")
    ok, failed = _records(client)[-2:]
    assert ok["ok"] and ok["extra"]["rows"] == 0
    assert not failed["ok"] and "rows" not in failed["extra"] and failed["extra"]["error"]


# --- the screen -------------------------------------------------------------------------------------------------------

def _start(client, run, **form):  # noqa: F811
    r = client.post("/reports/runs/start", data={"run": run, **form})
    st = client.app.state.gamgui
    job = st.jobs.get(st.audit_run_jobs.get(run, ""))
    if job is not None:
        wait_for_job(client, job)
    return unescape(r.text), job


def _page(client):  # noqa: F811
    return unescape(client.get("/reports/runs").text)


def test_loading_the_page_reads_nothing_and_offers_each_run(client, gam_calls):  # noqa: F811
    before = len(gam_calls())
    page = _page(client)
    assert len(gam_calls()) == before
    for run in runs.RUNS:
        assert f'id="run-{run.key}"' in page and run.title in page
    assert 'name="days"' in page and '<option value="90" selected>' in page
    assert 'href="/reports/runs"' in client.get("/reports").text


@pytest.mark.parametrize("run", runs.RUNS, ids=lambda r: r.key)
def test_every_step_of_every_run_leaves_one_f1_read_record(client, run):  # noqa: F811
    _start(client, run.key, days="90")
    records = _records(client)
    assert [r["extra"]["command"] for r in records] == [runs.step_command(run, s) for s in run.steps]
    for r in records:
        assert r["ok"] and r["target"] == "" and r["argv"][0] == "print"
        assert r["extra"]["origin"] == "audit_run" and r["extra"]["domain"] == "example.com" and r["extra"]["rows"] > 0


def test_the_inactive_run_reads_the_directory_once_records_it_and_offers_the_suspend_preview(client, gam_calls):  # noqa: F811
    before = len(gam_calls())
    _, job = _start(client, "inactive_users", days="90")
    calls = gam_calls()[before:]
    assert calls == [["print", "users", "fields", ",".join(CACHE_FIELDS), "formatjson"]] and gam_writes(calls) == []
    assert job is not None and (job.done, job.applied, job.error) == (1, 1, None)
    [record] = _records(client)
    assert record["ok"] and record["target"] == "" and record["argv"] == calls[0]
    assert record["extra"] == {"command": "inactive_users/directory", "origin": "audit_run", "rows": 3, "domain": "example.com"}
    page = _page(client)
    assert "Inactive 90+ days (2)" in page and ALICE in page and CAROL in page and BOB not in page
    assert 'hx-post="/reports/inactive/preview"' in page and 'name="days" value="90"' in page


def test_the_run_s_window_reaches_the_suspend_preview_and_apply_keeps_to_it(client, gam_calls):  # noqa: F811
    _start(client, "inactive_users", days="30")
    r = client.post("/reports/inactive/preview", data={"days": "30"})
    token = re.search(r'name="preview" value="([^"]+)"', r.text).group(1)
    assert "signed in for 30 days" in unescape(r.text) and 'name="days" value="30"' in r.text
    edited = client.post("/reports/inactive/apply", data={"preview": token, "confirmed": "1", "days": "90"})
    assert gam_writes(gam_calls()) == [] and "The form changed after the preview" in unescape(edited.text)   # not that window
    assert client.post("/reports/inactive/preview", data={"days": "45"}).text.count("Pick one of the windows") == 1


def test_a_window_the_run_doesn_t_offer_is_refused_before_any_read(client, gam_calls):  # noqa: F811
    before = len(gam_calls())
    for days in ("45", "", "ninety", "-90"):
        text, job = _start(client, "inactive_users", days=days)
        assert "Pick one of the windows" in text and job is None
    assert len(gam_calls()) == before
    text, job = _start(client, "no_such_run")
    assert "isn't one of the audit runs" in text and job is None


def test_the_license_run_records_both_steps_and_joins_them(client, gam_calls):  # noqa: F811
    before = len(gam_calls())
    _, job = _start(client, "license_usage")
    assert [a[:2] for a in gam_calls()[before:]] == [["print", "licenses"], ["print", "users"]]
    assert job.done == 2 and job.applied == 2
    records = {r["extra"]["command"]: r for r in _records(client)}
    assert records["license_usage/licenses"]["extra"] == {"command": "license_usage/licenses", "origin": "audit_run", "rows": 5,
                                                          "domain": "example.com"}
    assert records["license_usage/directory"]["extra"]["rows"] == 3
    result = client.app.state.gamgui.audit_runs["license_usage"]
    assert result.complete and set(result.tables) == {"licenses", "directory"}     # each step's table, kept for the run
    page = _page(client)
    assert "5 seats across 2 licenses." in page and "Seats on suspended accounts (2)" in page
    assert f'href="/users/detail?email={BOB.replace("@", "%40")}"' in page and LEFT in page


def test_a_refused_license_read_is_unknown_never_no_seats(client, gam_state):  # noqa: F811
    (gam_state / "nolicensing").touch()
    _, job = _start(client, "license_usage")
    assert job.failed_total == 1 and job.applied == 1     # the directory still read
    failed = _records(client, "license_usage/licenses")[-1]
    assert not failed["ok"] and "rows" not in failed["extra"] and "License Manager API" in failed["extra"]["error"]
    page = _page(client)
    assert ">unknown. A required API scope is not authorized" in page and "GAM’s error" in page and "Part of this run is unknown" in page
    assert "seats across" not in page and "No suspended account holds" not in page and "Every seat read" not in page
    r = client.get("/reports/runs/table.csv?run=license_usage&finding=on_suspended")
    assert r.status_code == 409 and "Not known" in r.text and "Email" not in r.text


def test_titles_alone_mean_gam_passed_over_every_product_unknown_not_none(client, gam_state):  # noqa: F811
    (gam_state / "licenses_silent").touch()
    _start(client, "license_usage")
    assert _records(client, "license_usage/licenses")[-1]["extra"]["rows"] == 0     # what GAM did, recorded as it was
    page = _page(client)
    assert "GAM listed no license seat at all" in page and "No suspended account holds" not in page


def test_a_product_gam_passed_over_reads_as_a_clean_run_so_every_table_carries_the_caveat(client, gam_state):  # noqa: F811
    (gam_state / "licenses_partial").touch()
    _, job = _start(client, "license_usage")
    assert job.failed_total == 0      # nothing in GAM's answer tells it apart from a whole read
    result = client.app.state.gamgui.audit_runs["license_usage"]
    assert [r[1] for r in result.finding("per_sku").rows] == [STANDARD]     # Google Vault's seat is simply not there
    page = _page(client)
    assert page.count("covers only the seats read") == 2 and "Check the counts against the Admin console" in page


def test_a_directory_that_stopped_short_is_unknown_and_its_partial_table_is_never_used(client, gam_state, gam_calls):  # noqa: F811
    (gam_state / "directory_fails").touch()
    _, job = _start(client, "license_usage")
    assert job.failed_total == 1
    result = client.app.state.gamgui.audit_runs["license_usage"]
    assert "directory" not in result.tables     # exit 50 wrote alice's row: not a directory
    per_sku, suspended, stray = result.findings
    assert per_sku.known and per_sku.rows[0][4:] == ("unknown", "unknown")
    assert not suspended.known and not stray.known
    _start(client, "inactive_users", days="90")
    page = _page(client)
    assert "Inactive 90+ days (" not in page and "Suspend these…" not in page and "Not known:" in page
    assert gam_writes(gam_calls()) == []


def test_stop_between_steps_leaves_the_rest_not_run_and_unknown(client, gam_calls):  # noqa: F811
    st = client.app.state.gamgui
    conn = st.connector
    real = conn.audit_read

    async def then_stop(command, argv, key):
        rows = await real(command, argv, key)
        st.jobs[st.audit_run_jobs["license_usage"]].cancel_requested = True     # Stop pressed during the first step
        return rows
    conn.audit_read = then_stop
    before = len(gam_calls())
    _, job = _start(client, "license_usage")
    assert [a[:2] for a in gam_calls()[before:]] == [["print", "licenses"]]    # the step in flight finished; none after
    assert job.error.startswith("Stopped by you") and [r["extra"]["command"] for r in _records(client)] == ["license_usage/licenses"]
    result = st.audit_runs["license_usage"]
    assert [r.state for r in result.reads] == [runs.READ, runs.NOT_RUN]
    assert not result.finding("on_suspended").known and "stopped before" in result.finding("on_suspended").why
    assert "not run. The run was stopped before this step." in _page(client)


def test_a_run_that_ends_after_a_switch_is_not_kept(client):  # noqa: F811
    st = client.app.state.gamgui
    conn = st.connector
    real = conn.audit_read

    async def then_switch(command, argv, key):
        rows = await real(command, argv, key)
        st.connector = object()    # another tenant's connector took over mid-run
        return rows
    conn.audit_read = then_switch
    _, job = _start(client, "inactive_users", days="90")
    assert job.error and "Not kept" in job.error and "inactive_users" not in st.audit_runs


def test_a_second_press_joins_the_running_run(client):  # noqa: F811
    st = client.app.state.gamgui
    gate = asyncio.Event()
    real = st.connector.audit_read

    async def held(command, argv, key):
        await gate.wait()
        return await real(command, argv, key)
    st.connector.audit_read = held
    first = client.post("/reports/runs/start", data={"run": "license_usage"})
    job = st.jobs[st.audit_run_jobs["license_usage"]]
    second = client.post("/reports/runs/start", data={"run": "license_usage"})
    assert len([j for j in st.jobs.values() if j.kind == "audit_run"]) == 1
    assert "Step 1 of 2" in unescape(first.text) and "<table" not in second.text and job.can_stop   # the poll draws no table
    assert job.status_url == f"/reports/runs/status?job={job.id}"
    assert 'id="run-license_usage"' in client.get(job.status_url).text
    client.portal.call(gate.set)
    wait_for_job(client, job)


def test_a_table_draws_a_bounded_number_of_rows_and_its_csv_is_whole_and_formula_safe(client):  # noqa: F811
    st = client.app.state.gamgui
    users = [_user(f"u{i:03}@example.com") for i in range(runs.ROWS_SHOWN + 30)] + [_user("=cmd@example.com")]
    reads = [_read(runs.DIRECTORY)]
    st.audit_runs["inactive_users"] = runs.RunResult(runs.INACTIVE_USERS, reads,
                                                     runs.findings_for(runs.INACTIVE_USERS, reads, {"directory": users}, days=90),
                                                     {"directory": users}, 0.0, 90)
    page = client.get("/reports/runs").text
    table = page.split('id="run-inactive_users-inactive"')[1].split("</table>")[0]
    assert table.count("<tr") == runs.ROWS_SHOWN + 1        # the titles and ROWS_SHOWN rows
    assert f"Showing the first {runs.ROWS_SHOWN} of {runs.ROWS_SHOWN + 31}" in page and " download href=" in page
    r = client.get("/reports/runs/table.csv?run=inactive_users&finding=inactive")
    lines = r.text.strip().splitlines()
    assert r.headers["content-disposition"] == 'attachment; filename="inactive-users-inactive.csv"'
    assert lines[0] == ",".join(runs.INACTIVE_COLUMNS) and len(lines) == runs.ROWS_SHOWN + 32
    assert any(line.startswith("'=cmd@example.com") for line in lines)
    assert client.get("/reports/runs/table.csv?run=inactive_users&finding=nope").status_code == 404


def test_a_switch_drops_the_kept_runs(client):  # noqa: F811
    st = client.app.state.gamgui
    _start(client, "inactive_users", days="90")
    assert "inactive_users" in st.audit_runs
    st.activate(st.connector)
    assert st.audit_runs == {} and st.audit_run_jobs == {}
