"""Audit runs, second half (plan F3): shared drive permissions, Classroom (read only) and the admin token review. Each
read's failing and partial exits are held to "unknown", never "none" (invariant 10): a drive GAM silently dropped, a 0
roster count, an admin whose apps GAM couldn't read."""

from __future__ import annotations

from datetime import datetime, timezone

from gamgui.core import audit_runs as runs
from gamgui.core.gam.models import GAMUser

from .test_audit_runs import _page, _read, _records, _start, _user
from .test_users_web import client  # noqa: F401  (the fixture)

NOW = datetime(2026, 11, 19, tzinfo=timezone.utc)
DOMAINS = ["example.com", "example.net", "alias.example.net"]


def _perm(drive, name, **p):
    return {"id": drive, "name": name, "permission": p}


def _uid(email, uid, **kw):
    u = _user(email, **kw)
    u.raw = {"id": uid}
    return u


# --- 2. Shared drive permissions --------------------------------------------------------------------------------------

def test_outside_members_come_first_and_an_alias_is_inside():
    # A drive's members are users and groups only (Google's sharing guide: membership "Applies to user or group only (not
    # domains)"), so the run flags members from outside the domain and claims nothing about links.
    acls = [_perm("D1", "Team", type="user", emailAddress="a@example.com", domain="example.com", role="organizer"),
            _perm("D1", "Team", type="user", emailAddress="x@alias.example.net", domain="alias.example.net", role="writer"),
            _perm("D1", "Team", type="group", emailAddress="g@partner.org", domain="partner.org", role="reader"),
            _perm("D2", "Ops", type="user", emailAddress="v@vendor.io", role="contentManager")]   # domain from the address
    reads = [_read(s) for s in runs.SHARED_DRIVES.steps]
    members = runs.shared_drive_findings([], acls, DOMAINS, [_user("a@example.com")], frozenset(), reads)[0]
    assert [(r[0], r[1], r[4]) for r in members.rows] == [
        ("Ops", "v@vendor.io", runs.EXTERNAL), ("Team", "g@partner.org", runs.EXTERNAL), ("Team", "a@example.com", ""),
        ("Team", "x@alias.example.net", "")]
    assert "2 are from outside the domain" in members.summary and "not its files' sharing" in members.note
    assert "anyone" not in members.summary


def test_a_drive_no_active_organizer_looks_after_is_flagged_and_a_group_organizer_is_not():
    users = [_user("active@example.com"), _user("gone@example.com", suspended=True), _user("leaver@example.com")]
    org = dict(type="user", role="organizer")
    acls = [_perm("A", "Fine", emailAddress="active@example.com", **org),
            _perm("B", "Suspended", emailAddress="gone@example.com", **org),
            _perm("C", "Leaving", emailAddress="leaver@example.com", **org),
            _perm("D", "Deleted", emailAddress="ghost@example.com", deleted=True, **org),
            _perm("E", "Nobody", type="user", emailAddress="active@example.com", role="writer"),
            _perm("F", "Group", type="group", emailAddress="team@example.com", role="organizer"),
            {"id": "G", "name": "Empty"}]                                  # read, with no permission at all
    reads = [_read(s) for s in runs.SHARED_DRIVES.steps]
    orphans = runs.shared_drive_findings([], acls, DOMAINS, users, frozenset({"leaver@example.com"}), reads)[1]
    assert {r[0]: r[2] for r in orphans.rows} == {
        "Suspended": "gone@example.com: suspended", "Leaving": "leaver@example.com: being offboarded",
        "Deleted": "ghost@example.com: not in the directory read", "Nobody": "no organizer at all",
        "Empty": "no organizer at all"}


def test_a_listed_drive_missing_from_the_permissions_is_unknown_not_unshared():
    drives = [{"id": "A", "name": "Read"}, {"id": "B", "name": "Dropped"}, {"id": "C", "name": "None at all"}]
    acls = [_perm("A", "Read", type="user", emailAddress="a@example.com", role="organizer"), {"id": "C", "name": "None at all"}]
    reads = [_read(s) for s in runs.SHARED_DRIVES.steps]
    unread = runs.shared_drive_findings(drives, acls, DOMAINS, [_user("a@example.com")], frozenset(), reads)[2]
    assert unread.rows == [("Dropped", "B")] and "unknown, not shared with no one" in unread.summary


def test_unknown_domains_or_directory_leave_their_tables_unknown():
    reads = [_read(runs.DRIVES), _read(runs.DRIVE_ACLS), _read(runs.DOMAINS, runs.UNKNOWN, "Refused."),
             _read(runs.DIRECTORY, runs.NOT_RUN)]
    perms, orphans, unread = runs.findings_for(runs.SHARED_DRIVES, reads, {"drives": [], "drive_acls": []})
    assert not perms.known and not orphans.known and unread.known and "stopped before" in orphans.why


def test_the_shared_drive_run_lists_outside_members_first_and_a_suspended_organizer(client):  # noqa: F811
    client.app.state.gamgui.offboard_jobs = {}
    _, job = _start(client, "shared_drive_permissions")
    assert job.applied == 4 and job.failed_total == 0
    page = _page(client)
    members = page.split('id="run-shared_drive_permissions-permissions"')[1].split("</table>")[0]
    assert members.index("pat@partner.org") < members.index("alice@example.com")
    assert "it-admins@example.com" in members and "Unused" not in members     # a drive with no member has no row
    assert "anyone with the link" not in members
    orphans = page.split('id="run-shared_drive_permissions-orphaned"')[1].split("</table>")[0]
    assert "Old projects" in orphans and "bob@example.com: suspended" in orphans and "Finance" not in orphans
    assert "GAM read every listed drive’s members." in page or "GAM read every listed drive's members." in page


def test_a_drive_gam_silently_dropped_is_named_and_an_offboarding_makes_a_leaver(client, gam_state):  # noqa: F811
    (gam_state / "acl_dropped").mkdir()
    (gam_state / "acl_dropped" / "0ALEGALuk9PVA").touch()
    client.app.state.gamgui.offboard_jobs = {"alice@example.com": "job"}
    _start(client, "shared_drive_permissions")
    result = client.app.state.gamgui.audit_runs["shared_drive_permissions"]
    assert result.finding("unread").rows == [("Legal hold", "0ALEGALuk9PVA")]
    # alice is being offboarded; Unused has no member at all, so no organizer either
    assert {r[0] for r in result.finding("orphaned").rows} == {"Finance", "Old projects", "Unused"}


def test_no_shared_drive_is_gam_s_own_none_and_a_refused_list_is_unknown(client, gam_state):  # noqa: F811
    (gam_state / "nodrives").touch()
    _, job = _start(client, "shared_drive_permissions")
    assert job.failed_total == 0                          # exit 60 with the titles: the domain has none
    result = client.app.state.gamgui.audit_runs["shared_drive_permissions"]
    assert result.finding("permissions").known and result.finding("permissions").rows == []
    (gam_state / "nodrives").unlink()
    (gam_state / "denied").mkdir()
    (gam_state / "denied" / "drives").touch()
    _, job = _start(client, "shared_drive_permissions")
    assert job.failed_total == 2
    result = client.app.state.gamgui.audit_runs["shared_drive_permissions"]
    assert not any(f.known for f in result.findings)
    assert [r["ok"] for r in _records(client, "shared_drive_permissions/drives")] == [True, False]


# --- 4. Classroom ---------------------------------------------------------------------------------------------------------

def test_classes_join_their_owner_by_id_and_flag_suspended_owners_strays_and_stale_ones():
    users = [_uid("t@x.com", "1"), _uid("gone@x.com", "2", suspended=True)]
    courses = [{"id": "c1", "name": "Bio", "ownerId": "1", "courseState": "ACTIVE", "updateTime": "2026-10-01T00:00:00Z",
                "teachers": 1, "students": 0},
               {"id": "c2", "name": "Old", "ownerId": "2", "courseState": "ACTIVE", "updateTime": "2024-01-01T00:00:00Z"},
               {"id": "c3", "name": "Kept", "ownerId": "9", "courseState": "ARCHIVED", "updateTime": "2020-01-01T00:00:00Z"},
               {"id": "c4", "name": "Never", "ownerId": "1", "courseState": "PROVISIONED", "updateTime": "2025-01-01T00:00:00Z"}]
    every, owned, strays, stale = runs.classroom_findings(courses, users, [_read(s) for s in runs.CLASSROOM.steps], NOW)
    assert [(r[0], r[2], r[4], r[5]) for r in every.rows][0] == ("Bio", "t@x.com", "1", "0")
    assert "isn't an empty class" in every.note and "Not yet checked on a school domain" in every.note
    assert owned.rows == [("gone@x.com", "Old", "ACTIVE", "2024-01-01")] and strays.rows == [("Kept", "9", "ARCHIVED")]
    assert [r[0] for r in stale.rows] == ["Old", "Never"]          # archived isn't stale; provisioned a year on is


def test_an_unknown_directory_shows_owner_ids_and_never_says_no_suspended_owner():
    reads = [_read(runs.COURSES), _read(runs.DIRECTORY, runs.UNKNOWN, "Refused.")]
    every, owned, strays, _ = runs.findings_for(runs.CLASSROOM, reads, {"courses": [{"name": "Bio", "ownerId": "7"}]}, now=NOW)
    assert every.known and every.rows[0][2] == "7" and not owned.known and not strays.known


def test_the_classroom_run_reads_every_state_with_counts_and_names_an_owner_the_directory_lacks(client, gam_calls):  # noqa: F811
    _, job = _start(client, "classroom_courses")
    assert job.failed_total == 0
    [courses] = [a for a in gam_calls() if a[:2] == ["print", "courses"]]
    assert "active,archived,provisioned,declined,suspended" in courses and courses[-6:-3] == ["show", "all", "countsonly"]
    result = client.app.state.gamgui.audit_runs["classroom_courses"]
    assert ("World History 2025", "100000000000000000009", "ARCHIVED") in result.finding("owner_not_in_directory").rows
    assert any(r[0] == "Biology" and r[2] == "alice@example.com" and r[5] != "0" for r in result.finding("courses").rows)


def test_a_roster_gam_couldn_t_read_counts_zero_and_a_classroom_refusal_is_unknown(client, gam_state):  # noqa: F811
    (gam_state / "roster_gone").mkdir()
    (gam_state / "roster_gone" / "500000000001").touch()
    _start(client, "classroom_courses")
    bio = next(r for r in client.app.state.gamgui.audit_runs["classroom_courses"].finding("courses").rows if r[0] == "Biology")
    assert bio[4:6] == ("0", "0") and "isn't an empty class" in _page(client)
    for trigger in ("noclassroom", "classroom_silent"):     # exit 15; exit 0 with no table
        (gam_state / trigger).touch()
        _, job = _start(client, "classroom_courses")
        assert job.failed_total == 1
        assert not client.app.state.gamgui.audit_runs["classroom_courses"].finding("courses").known
        (gam_state / trigger).unlink()


# --- 5. Admin token review ------------------------------------------------------------------------------------------------

ADMIN_ROWS = [{"roleAssignmentId": "1", "role": "_SEED_ADMIN_ROLE", "assignedToUser": "alice@example.com"},
              {"roleAssignmentId": "2", "role": "_HELP_DESK_ADMIN_ROLE", "assignedToUser": "bob@example.com",
               "assignedToGroup": "it-admins@example.com"},
              {"roleAssignmentId": "3", "role": "_GROUPS_ADMIN_ROLE", "assignedToServiceAccount": "bot@p.iam.gserviceaccount.com"},
              {"roleAssignmentId": "4", "role": "_X", "assignedToGroup": "empty@example.com"}]


def test_the_admins_are_the_people_rows_name_and_the_token_read_asks_for_them_alone():
    assert runs.admin_addresses(ADMIN_ROWS) == ["alice@example.com", "bob@example.com"]
    assert runs.step_argv(runs.ADMIN_TOKENS, {"admins": ADMIN_ROWS}) == [
        "print", "tokens", "users", "alice@example.com,bob@example.com"]
    assert runs.step_argv(runs.ADMIN_TOKENS, {}) is None         # no admins table: nothing to read, never "no apps"


def test_suspended_admins_are_kept_and_ai_and_broad_access_flagged_first():
    users = [_user("alice@example.com"), _user("bob@example.com", suspended=True)]
    tokens = [{"user": "alice@example.com", "clientId": "1", "displayText": "Zoom",
               "scopes": "https://www.googleapis.com/auth/calendar.readonly"},
              {"user": "bob@example.com", "clientId": "2", "displayText": "ChatGPT", "scopes": "https://mail.google.com/"}]
    admins, apps = runs.admin_token_findings(ADMIN_ROWS, users, tokens, [_read(s) for s in runs.ADMIN_TOKEN_REVIEW.steps])
    assert admins.rows == [("alice@example.com", "_SEED_ADMIN_ROLE", "no", "1"),
                           ("bob@example.com", "_HELP_DESK_ADMIN_ROLE (through it-admins@example.com)", "yes", "1")]
    assert apps.rows[0][:2] == ("bob@example.com", "ChatGPT")
    assert apps.rows[0][3] == "suspended admin, AI, by its name, broad access" and apps.rows[1][3] == ""


def test_an_admin_gam_couldn_t_read_has_unknown_apps_never_none():
    reads = [_read(runs.ADMINS), _read(runs.DIRECTORY),
             runs.StepRead(runs.ADMIN_TOKENS, runs.PARTIAL, "", rows=0, unread=("bob@example.com",))]
    admins, apps = runs.admin_token_findings(ADMIN_ROWS, [_user("alice@example.com"), _user("bob@example.com")], [], reads)
    assert dict((r[0], r[3]) for r in admins.rows) == {"alice@example.com": "0", "bob@example.com": "unknown"}
    assert "couldn't read the apps of bob@example.com" in apps.note
    reads[2] = _read(runs.ADMIN_TOKENS, runs.UNKNOWN, "Refused.")
    admins, apps = runs.admin_token_findings(ADMIN_ROWS, [], [], reads)
    assert {r[3] for r in admins.rows} == {"unknown"} and not apps.known


def test_the_admin_run_reads_suspended_admins_and_leaves_other_people_out(client, gam_calls):  # noqa: F811
    _, job = _start(client, "admin_tokens")
    assert job.applied == 3
    assert ["print", "tokens", "users", "alice@example.com,bob@example.com"] in gam_calls()
    result = client.app.state.gamgui.audit_runs["admin_tokens"]
    apps = result.finding("admin_apps").rows
    assert {r[0] for r in apps} == {"alice@example.com", "bob@example.com"}      # carol's grants aren't an admin's
    assert any(r[1] == "Only Bob Has This" and "suspended admin" in r[3] for r in apps)
    assert "Incident" in _page(client)


def test_an_admin_whose_apps_gam_couldn_t_read_is_partial_and_recorded_so(client, gam_state):  # noqa: F811
    (gam_state / "tokens_gone").mkdir()
    (gam_state / "tokens_gone" / "bob@example.com").touch()
    _start(client, "admin_tokens")
    record = _records(client, "admin_tokens/admin_tokens")[-1]
    assert record["ok"] and record["extra"]["partial"] is True
    result = client.app.state.gamgui.audit_runs["admin_tokens"]
    assert result.reads[2].state == runs.PARTIAL and not result.complete
    assert dict((r[0], r[3]) for r in result.finding("admins").rows)["bob@example.com"] == "unknown"
    assert "bob@example.com, and read the rest" in _page(client)


def test_refused_admins_mean_the_token_read_never_runs(client, gam_state, gam_calls):  # noqa: F811
    (gam_state / "denied").mkdir()
    (gam_state / "denied" / "admins").touch()
    _, job = _start(client, "admin_tokens")
    assert job.failed_total == 2 and not [a for a in gam_calls() if a[:2] == ["print", "tokens"]]
    assert [r["extra"]["command"] for r in _records(client)] == ["admin_tokens/admins", "admin_tokens/directory"]
    assert "an earlier step found" in _page(client)


UNNAMED = [{"roleAssignmentId": "5", "role": "_USER_MANAGEMENT_ADMIN_ROLE", "assignedTo": "3", "assignedToUnknown": "True"},
           {"roleAssignmentId": "6", "role": "_HELP_DESK_ADMIN_ROLE", "assignedTo": "77", "assignedToUnknown": "True",
            "assignedToGroup": "it-admins@example.com"}]


def test_a_holder_gam_couldn_t_name_is_joined_by_id_or_kept_as_unknown_never_dropped():
    users = [_uid("alice@example.com", "1"), _uid("carol@example.com", "3")]
    rows = ADMIN_ROWS + UNNAMED
    assert runs.admin_addresses(rows, users) == ["alice@example.com", "bob@example.com", "carol@example.com"]
    assert runs.unnamed_holders(rows, users) == ["77"]
    assert runs.step_argv(runs.ADMIN_TOKENS, {"admins": rows, "directory": users})[-1] == \
        "alice@example.com,bob@example.com,carol@example.com"
    admins, apps = runs.admin_token_findings(rows, users, [], [_read(s) for s in runs.ADMIN_TOKEN_REVIEW.steps])
    table = {r[0]: r for r in admins.rows}
    assert table["carol@example.com"][1] == "_USER_MANAGEMENT_ADMIN_ROLE" and table["carol@example.com"][3] == "0"
    assert table["ID 77"] == ("ID 77", "_HELP_DESK_ADMIN_ROLE (through it-admins@example.com)", "unknown", "unknown")
    assert admins.partial and apps.partial and "couldn't name 1 role holder" in admins.note
    result = runs.RunResult(runs.ADMIN_TOKEN_REVIEW, [_read(s) for s in runs.ADMIN_TOKEN_REVIEW.steps], [admins, apps], {}, 0.0)
    assert not result.complete       # every step read, yet a holder is unknown: never "every step was read" alone


def test_the_admin_run_reads_a_holder_gam_couldn_t_name_and_shows_the_rest_as_unknown(client, gam_state, gam_calls):  # noqa: F811
    (gam_state / "admin_unnamed").touch()
    _start(client, "admin_tokens")
    assert ["print", "tokens", "users", "alice@example.com,bob@example.com,carol@example.com"] in gam_calls()
    result = client.app.state.gamgui.audit_runs["admin_tokens"]
    assert not result.complete and ("ID 100000000000000000077" in {r[0] for r in result.finding("admins").rows})
    page = _page(client)
    assert "Part of this run is unknown" in page and "ID 100000000000000000077" in page
    assert 'email=ID' not in page        # a holder with no address links to nobody


def test_a_warning_exit_that_names_nobody_is_unknown_not_a_whole_read(client, monkeypatch):  # noqa: F811
    from gamgui.core.gam.runner import RunResult
    conn = client.app.state.gamgui.connector
    real = conn.runner.run_any_exit

    async def unnamed_warning(domain, argv, timeout=None):
        res = await real(domain, argv, timeout=timeout)
        if argv[:3] == ["print", "tokens", "users"]:
            return RunResult(stdout=res.stdout, stderr="", returncode=56)
        return res
    monkeypatch.setattr(conn.runner, "run_any_exit", unnamed_warning)
    _, job = _start(client, "admin_tokens")
    record = _records(client, "admin_tokens/admin_tokens")[-1]
    assert not record["ok"] and "without naming" in record["extra"]["error"] and "rows" not in record["extra"]
    result = client.app.state.gamgui.audit_runs["admin_tokens"]
    assert result.reads[2].state == runs.UNKNOWN and not result.finding("admin_apps").known


def test_no_new_run_offers_a_write(client):  # noqa: F811
    for run in ("shared_drive_permissions", "classroom_courses", "admin_tokens"):
        _start(client, run)
    page = _page(client)
    assert page.count('hx-post="/reports/inactive/preview"') == 0 and "GAMUser" not in page
    assert isinstance(client.app.state.gamgui.audit_runs["admin_tokens"].tables["directory"][0], GAMUser)
