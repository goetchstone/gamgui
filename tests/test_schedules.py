"""Schedules (plan F7a): an audit run that says when it's due, on Home and on its own page, run on the operator's click.

Nothing runs on its own. A scheduled run that read every step keeps its tables on this Mac, bounded by a count the
operator picks, so the next run can say what changed. A run that failed or stopped short never counts, is never kept and
never makes "no change" (invariant 10); a schedules file that can't be read is unknown and never saved over, and one that
isn't a store is set aside (failure-log 2026-11-18). Kept tables are tenant data at rest: 0700 folders, 0600 files.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import date, datetime, timezone
from html import unescape
from pathlib import Path

import pytest

from gamgui.core import audit_runs as runs
from gamgui.core import schedules as sched
from gamgui.core.connectors.gam_connector import GAMConnector

from .helpers import gam_writes, wait_for_job
from .test_setup_web import _store, ctx  # noqa: F401  (the fixture)
from .test_users_web import client  # noqa: F401  (the fixture)

DAY = 86_400.0
T0 = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc).timestamp()     # a Wednesday


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    monkeypatch.setattr(sched, "TZ", timezone.utc)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _finding(key, rows, known=True, partial=False, columns=("Email", "Name")):
    return runs.Finding(key, key.title(), "", columns, [tuple(r) for r in rows], known=known, partial=partial)


# --- the cadence and "due" ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("last, cadence, due", [
    (date(2026, 11, 18), "daily", date(2026, 11, 19)),
    (date(2026, 12, 31), "daily", date(2027, 1, 1)),
    (date(2026, 11, 18), "weekly", date(2026, 11, 25)),
    (date(2026, 11, 18), "monthly", date(2026, 12, 18)),
    (date(2026, 12, 18), "monthly", date(2027, 1, 18)),
    (date(2026, 1, 31), "monthly", date(2026, 2, 28)),      # the 31st into a shorter month: its last day
    (date(2028, 1, 31), "monthly", date(2028, 2, 29)),
    (date(2026, 3, 31), "monthly", date(2026, 4, 30)),
])
def test_a_run_is_due_again_a_day_a_week_or_a_calendar_month_after_the_day_it_counted(last, cadence, due):
    assert sched.next_day(last, cadence) == due


def test_due_is_a_local_day_not_a_count_of_hours():
    s = sched.Schedule("license_usage", "daily", 5, since=T0 - DAY, done=T0)            # counted Wed 09:00
    assert not sched.is_due(s, T0 + 14 * 3600)                                           # Wed 23:00: same day
    assert sched.is_due(s, T0 + 15 * 3600)                                               # Thu 00:00: due
    weekly = sched.Schedule("license_usage", "weekly", 5, since=T0, done=T0)
    assert not sched.is_due(weekly, T0 + 6 * DAY) and sched.is_due(weekly, T0 + 7 * DAY - 9 * 3600)


def test_never_counted_since_it_was_scheduled_is_due_now_and_the_words_say_why():
    fresh = sched.Schedule("license_usage", "weekly", 5, since=T0)
    words = sched.due_words("License usage", fresh, T0)
    assert words.due and words.headline == "License usage is due"
    assert words.line == "License usage is due. Weekly; no run of it has read every step since it was scheduled."
    done = sched.Schedule("license_usage", "weekly", 5, since=T0, done=T0)
    assert sched.due_words("License usage", done, T0 + DAY) == sched.Due(False, "Weekly, next due Wed 25 Nov 2026")
    late = sched.due_words("License usage", done, T0 + 9 * DAY)
    assert late.due and late.detail == "Weekly; its last run that read every step was on Wed 18 Nov 2026."
    assert sched.when_words(T0) == "Wed 18 Nov 2026, 09:00"


# --- the schedules file -------------------------------------------------------------------------------------------------

def test_schedules_are_kept_per_domain_in_a_private_file(tmp_path):
    store = sched.ScheduleStore(tmp_path / "s")
    saved = store.put("Example.COM", "license_usage", "weekly", 3, T0)
    assert saved == sched.Schedule("license_usage", "weekly", 3, T0, 0.0)
    assert store.schedules("example.com") == {"license_usage": saved}
    assert store.schedules("example.org") == {}
    assert _mode(tmp_path / "s") == 0o700 and _mode(store.path) == 0o600
    assert json.loads(store.path.read_text())["domains"] == {
        "example.com": {"license_usage": {"cadence": "weekly", "keep": 3, "since": T0, "done": 0.0}}}


def test_a_change_of_cadence_keeps_when_it_last_counted_and_unscheduling_forgets_it(tmp_path):
    store = sched.ScheduleStore(tmp_path / "s")
    store.mark_done("example.com", "license_usage", T0)          # not scheduled: nothing to mark
    assert store.schedules("example.com") == {}
    store.put("example.com", "license_usage", "weekly", 3, T0)
    store.mark_done("example.com", "license_usage", T0 + DAY)
    changed = store.put("example.com", "license_usage", "monthly", 4, T0 + 2 * DAY)
    assert changed == sched.Schedule("license_usage", "monthly", 4, T0, T0 + DAY)
    assert store.put("example.com", "license_usage", "", 0, T0) is None
    assert store.schedules("example.com") == {}


def test_a_schedule_that_isn_t_one_is_refused(tmp_path):
    store = sched.ScheduleStore(tmp_path / "s")
    for run, cadence, keep in (("license_usage", "hourly", 3), ("license_usage", "weekly", 0),
                               ("license_usage", "weekly", sched.KEEP_CAP + 1), ("../x", "weekly", 3)):
        with pytest.raises(ValueError):
            store.put("example.com", run, cadence, keep, T0)
    assert not store.path.exists()


def test_a_schedules_file_the_disk_couldn_t_read_is_unknown_and_never_saved_over(tmp_path):
    store = sched.ScheduleStore(tmp_path / "s")
    store.put("example.com", "license_usage", "weekly", 3, T0)
    before = store.path.read_bytes()
    os.chmod(store.path, 0)
    try:
        with pytest.raises(sched.StoreProblem, match="couldn't be read just now"):
            store.schedules("example.com")
        with pytest.raises(sched.StoreProblem):
            store.put("example.com", "classroom_courses", "daily", 1, T0)
        with pytest.raises(sched.StoreProblem):
            store.mark_done("example.com", "license_usage", T0)
    finally:
        os.chmod(store.path, 0o600)
    assert store.path.read_bytes() == before and store.set_aside is None


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe\x00", b'{"version": 1, "domains": []}',
                                     b'{"version": 2, "domains": {}}',
                                     b'{"version": 1, "domains": {"example.com": {"license_usage": {"cadence": "hourly",'
                                     b' "keep": 3, "since": 1, "done": 0}}}}',
                                     b'{"version": 1, "domains": {"Example.com": {}}}'],
                         ids=["not-json", "not-utf8", "wrong-shape", "wrong-version", "bad-cadence", "unlowered-domain"])
def test_a_schedules_file_that_isn_t_a_store_is_set_aside_before_anything_is_saved(tmp_path, content):
    store = sched.ScheduleStore(tmp_path / "s")
    store.root.mkdir()
    store.path.write_bytes(content)
    assert store.schedules("example.com") == {}
    assert store.set_aside is not None and store.set_aside.read_bytes() == content
    store.put("example.com", "license_usage", "weekly", 3, T0)
    assert store.set_aside.read_bytes() == content                      # the operator's file, as it was
    assert set(store.schedules("example.com")) == {"license_usage"}


@pytest.mark.parametrize("content", [
    b'{"version": 1, "domains": {"example.com": {"license_usage": {"cadence": "weekly", "keep": 3, "since": 1, "done": 1e300}}}}',
    b'{"version": 1, "domains": {"example.com": {"license_usage": {"cadence": "weekly", "keep": 3, "since": 1' + b"0" * 400 + b'}}}}',
    b'{"version": 1, "domains": {"example.com": {"license_usage": {"cadence": "weekly", "keep": 3, "since": NaN}}}}',
    b"[" * 100_000,
], ids=["huge-done", "huge-since", "nan", "deep"])
def test_a_time_no_date_can_be_made_of_or_a_file_too_deep_to_read_is_set_aside_never_a_crash(tmp_path, content):
    store = sched.ScheduleStore(tmp_path / "s")
    store.root.mkdir()
    store.path.write_bytes(content)
    assert store.schedules("example.com") == {} and store.set_aside.read_bytes() == content


def test_a_schedule_for_no_domain_is_refused_before_anything_is_saved(tmp_path):
    store = sched.ScheduleStore(tmp_path / "s")
    store.put("example.com", "license_usage", "weekly", 3, T0)
    for domain in ("", "   "):
        with pytest.raises(ValueError):
            store.put(domain, "license_usage", "weekly", 3, T0)
    assert set(store.schedules("example.com")) == {"license_usage"} and store.set_aside is None


def test_a_file_that_isn_t_a_store_and_can_t_be_moved_aside_is_never_saved_over(tmp_path, monkeypatch):
    store = sched.ScheduleStore(tmp_path / "s")
    store.root.mkdir()
    store.path.write_bytes(b"{not json")

    def refuse(*_a, **_k):
        raise PermissionError("no")
    monkeypatch.setattr(sched.os, "replace", refuse)
    with pytest.raises(sched.StoreProblem, match="couldn't be moved aside"):
        store.put("example.com", "license_usage", "weekly", 3, T0)
    assert store.path.read_bytes() == b"{not json"


# --- kept runs ----------------------------------------------------------------------------------------------------------

def test_a_kept_run_is_whole_private_and_listed_newest_first(tmp_path):
    kept = sched.KeptRuns(tmp_path / "s" / "kept")
    first = kept.keep("example.com", "license_usage", T0, 0, [_finding("seats", [("a@x.com", "A")])])
    second = kept.keep("example.com", "license_usage", T0 + DAY, 0,
                       [_finding("seats", [("a@x.com", "A"), ("b@x.com", "B")]), _finding("gone", [], known=False)])
    assert [k.id for k in kept.runs("example.com", "license_usage")] == [second.id, first.id]
    head = kept.runs("example.com", "license_usage")[0]
    assert head.when == T0 + DAY and head.table("seats").count == 2 and head.table("seats").rows is None   # the header alone
    assert not head.table("gone").known and not head.table("gone").kept
    whole = kept.load("example.com", "license_usage", second.id)
    assert whole.table("seats").rows == [("a@x.com", "A"), ("b@x.com", "B")]
    assert kept.runs("example.org", "license_usage") == [] and kept.runs("example.com", "classroom_courses") == []
    for folder in (tmp_path / "s", kept.root, *[p for p in kept.root.rglob("*") if p.is_dir()]):
        assert _mode(folder) == 0o700
    files = list(kept.root.rglob("*.jsonl"))
    assert len(files) == 2 and all(_mode(f) == 0o600 for f in files)
    assert "example.com" not in str(files[0])      # the folder is the domain's hash


def test_a_kept_run_that_can_t_be_read_is_listed_as_such_never_dropped_or_read_as_none(tmp_path):
    kept = sched.KeptRuns(tmp_path / "kept")
    good = kept.keep("example.com", "license_usage", T0, 0, [_finding("seats", [("a@x.com", "A")])])
    bad = kept.keep("example.com", "license_usage", T0 + DAY, 0, [_finding("seats", [("a@x.com", "A"), ("b", "B")])])
    path = next(p for p in kept.root.rglob("*.jsonl") if p.stem == bad.id)
    lines = path.read_text().splitlines()
    path.write_text(lines[0] + "\n" + lines[1][:20] + "\n")             # its rows cut off
    listed = kept.runs("example.com", "license_usage")
    assert [k.problem for k in listed] == ["", ""]                       # the header still reads
    assert kept.load("example.com", "license_usage", bad.id).problem == "It isn't whole."
    path.write_text("garbage\n")
    assert kept.runs("example.com", "license_usage")[0].problem == "It isn't whole."
    os.chmod(path, 0)
    try:
        assert kept.runs("example.com", "license_usage")[0].problem == "It couldn't be read just now."
    finally:
        os.chmod(path, 0o600)
    assert kept.load("example.com", "license_usage", good.id).table("seats").rows == [("a@x.com", "A")]
    assert kept.load("example.com", "license_usage", "../../x").problem == "It is no longer kept."


def test_a_link_in_the_kept_folder_is_never_read(tmp_path):
    kept = sched.KeptRuns(tmp_path / "kept")
    run = kept.keep("example.com", "license_usage", T0, 0, [_finding("seats", [("a", "A")])])
    folder = next(p for p in kept.root.rglob("*.jsonl")).parent
    secret = tmp_path / "secret.jsonl"
    secret.write_text("x\n")
    (folder / "9999999999999-deadbeef.jsonl").symlink_to(secret)
    assert [k.id for k in kept.runs("example.com", "license_usage")] == [run.id]


def test_a_kept_header_whose_numbers_aren_t_a_time_or_a_window_is_listed_as_not_whole(tmp_path):
    kept = sched.KeptRuns(tmp_path / "kept")
    run = kept.keep("example.com", "license_usage", T0, 90, [_finding("seats", [("a", "A")])])
    path = next(kept.root.rglob("*.jsonl"))
    lines = path.read_text().splitlines()
    for field, value in (("days", "Infinity"), ("days", "true"), ("days", "1e9"), ("when", "NaN"), ("when", "1e300")):
        head = json.loads(lines[0])
        head[field] = "__X__"
        path.write_text(json.dumps(head).replace('"__X__"', value) + "\n" + "\n".join(lines[1:]) + "\n")
        assert [k.problem for k in kept.runs("example.com", "license_usage")] == ["It isn't whole."], (field, value)
        assert kept.load("example.com", "license_usage", run.id).problem == "It isn't whole."


def test_a_link_anywhere_in_the_kept_folders_is_refused_and_what_it_points_at_left_alone(tmp_path):
    outside = tmp_path / "outside"
    kept = sched.KeptRuns(outside / "kept")
    kept.keep("example.com", "license_usage", T0, 0, [_finding("seats", [("a", "A")])])
    victim = next(outside.rglob("*.jsonl"))
    real_kept = outside / "kept"
    top = next(real_kept.iterdir())
    for linked in ("schedules", "kept", "domain", "run"):
        root = tmp_path / linked / "schedules"
        root.parent.mkdir()
        if linked == "schedules":
            root.symlink_to(outside)                               # the store's own folder is a link
        else:
            root.mkdir()
            if linked == "kept":
                (root / "kept").symlink_to(real_kept)
            else:
                (root / "kept").mkdir()
                if linked == "domain":
                    (root / "kept" / top.name).symlink_to(top)
                else:
                    (root / "kept" / top.name).mkdir()
                    (root / "kept" / top.name / "license_usage").symlink_to(top / "license_usage")
        probe = sched.KeptRuns(root / "kept")
        for act, *args in ((probe.delete_all,), (probe.prune, 0), (probe.runs,), (probe.keep, T0, 0, [])):
            with pytest.raises(sched.StoreProblem, match="isn't a folder"):
                act("example.com", "license_usage", *args)
        if linked == "run":       # Remove lists the domain's run folders and passes a link over: nothing to follow
            assert probe.forget_domain("example.com") == 0
        else:
            with pytest.raises(sched.StoreProblem, match="isn't a folder"):
                probe.forget_domain("example.com")
        assert victim.exists(), linked


def test_prune_keeps_the_newest_and_delete_and_forget_leave_nothing(tmp_path):
    kept = sched.KeptRuns(tmp_path / "kept")
    ids = [kept.keep("example.com", "license_usage", T0 + i * DAY, 0, [_finding("seats", [("a", "A")])]).id for i in range(5)]
    kept.keep("example.com", "classroom_courses", T0, 0, [_finding("courses", [("c", "C")])])
    kept.keep("example.org", "license_usage", T0, 0, [_finding("seats", [("o", "O")])])
    assert kept.prune("example.com", "license_usage", 2) == 3
    assert [k.id for k in kept.runs("example.com", "license_usage")] == ids[:-3:-1]
    assert kept.delete_all("example.com", "license_usage") == 2 and kept.runs("example.com", "license_usage") == []
    assert kept.forget_domain("Example.com") == 1
    assert kept.runs("example.com", "classroom_courses") == []
    assert len(kept.runs("example.org", "license_usage")) == 1         # another domain's: untouched


def test_a_table_over_the_row_cap_isn_t_kept_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "KEEP_ROWS_CAP", 2)
    kept = sched.KeptRuns(tmp_path / "kept")
    run = kept.keep("example.com", "license_usage", T0, 0,
                    [_finding("big", [("a", "1"), ("b", "2"), ("c", "3")]), _finding("small", [("a", "1")])])
    whole = kept.load("example.com", "license_usage", run.id)
    assert whole.table("big").count == 3 and not whole.table("big").kept and whole.table("big").rows is None
    assert whole.table("small").rows == [("a", "1")]


# --- changed since ------------------------------------------------------------------------------------------------------

def _compare(kept, findings, days=0):
    return sched.changes_since(findings, days, kept.runs("example.com", "license_usage"),
                               lambda i: kept.load("example.com", "license_usage", i))


def test_new_and_gone_are_whole_rows_counted_as_a_multiset(tmp_path):
    kept = sched.KeptRuns(tmp_path / "kept")
    kept.keep("example.com", "license_usage", T0, 0,
              [_finding("seats", [("a", "A"), ("b", "B"), ("dup", "D"), ("dup", "D")])])
    now = [_finding("seats", [("a", "A"), ("b", "Bee"), ("dup", "D"), ("c", "C")])]
    change = _compare(kept, now)["seats"]
    assert change.compared and change.since == T0
    assert change.new == [("b", "Bee"), ("c", "C")] and change.gone == [("b", "B"), ("dup", "D")]
    same = _compare(kept, [_finding("seats", [("dup", "D"), ("b", "B"), ("dup", "D"), ("a", "A")])])["seats"]
    assert same.compared and same.new == [] and same.gone == []


def test_a_table_unknown_or_partly_unknown_now_is_never_compared(tmp_path):
    kept = sched.KeptRuns(tmp_path / "kept")
    kept.keep("example.com", "license_usage", T0, 0, [_finding("seats", [("a", "A")])])
    out = _compare(kept, [_finding("seats", [], known=False)])
    assert not out["seats"].compared and out["seats"].why == sched.NOT_KNOWN_NOW and out["seats"].gone == []
    out = _compare(kept, [_finding("seats", [], partial=True)])
    assert not out["seats"].compared and out["seats"].why == sched.PART_UNKNOWN_NOW


def test_it_compares_with_the_newest_run_that_read_the_table_whole_with_the_same_window(tmp_path, monkeypatch):
    monkeypatch.setattr(sched, "KEEP_ROWS_CAP", 3)
    kept = sched.KeptRuns(tmp_path / "kept")
    kept.keep("example.com", "license_usage", T0, 90, [_finding("seats", [("whole", "90")])])
    kept.keep("example.com", "license_usage", T0 + 1, 30, [_finding("seats", [("other", "window")])])
    kept.keep("example.com", "license_usage", T0 + 2, 90, [_finding("seats", [("x", "1")], partial=True)])
    kept.keep("example.com", "license_usage", T0 + 3, 90, [_finding("seats", [], known=False)])
    kept.keep("example.com", "license_usage", T0 + 4, 90, [_finding("seats", [("a", "1")] * 4)])      # over the cap
    broken = kept.keep("example.com", "license_usage", T0 + 5, 90, [_finding("seats", [("broken", "1")])])
    next(p for p in kept.root.rglob("*.jsonl") if p.stem == broken.id).write_text("garbage\n")
    change = _compare(kept, [_finding("seats", [("whole", "90")])], days=90)["seats"]
    assert change.compared and change.since == T0 and change.new == [] and change.gone == []
    nothing = _compare(kept, [_finding("seats", [("a", "1")])], days=180)["seats"]
    assert not nothing.compared and nothing.why == sched.NOTHING_BEFORE


# --- after a run --------------------------------------------------------------------------------------------------------

def _stores(tmp_path):
    return sched.ScheduleStore(tmp_path / "s"), sched.KeptRuns(tmp_path / "s" / "kept")


def test_an_unscheduled_run_keeps_and_compares_nothing(tmp_path):
    store, kept = _stores(tmp_path)
    after = sched.after_run(store, kept, "example.com", "license_usage", [_finding("seats", [("a", "A")])], 0, True, T0)
    assert after == sched.AfterRun({}) and not kept.root.exists()


def test_a_scheduled_run_that_counts_is_kept_pruned_and_marked_done(tmp_path):
    store, kept = _stores(tmp_path)
    store.put("example.com", "license_usage", "weekly", 2, T0)
    for i in range(3):
        after = sched.after_run(store, kept, "example.com", "license_usage", [_finding("seats", [("a", str(i))])], 0,
                                True, T0 + i)
        assert not after.problem and after.note == "Kept on this Mac, as one of the last 2 runs it keeps."
    assert [k.when for k in kept.runs("example.com", "license_usage")] == [T0 + 2, T0 + 1]
    assert store.schedules("example.com")["license_usage"].done == T0 + 2
    assert after.changes["seats"].new == [("a", "2")] and after.changes["seats"].gone == [("a", "1")]


def test_a_run_with_a_step_unanswered_is_compared_where_it_can_be_but_never_kept_or_counted(tmp_path):
    store, kept = _stores(tmp_path)
    store.put("example.com", "license_usage", "weekly", 2, T0)
    sched.after_run(store, kept, "example.com", "license_usage", [_finding("seats", [("a", "A")])], 0, True, T0)
    after = sched.after_run(store, kept, "example.com", "license_usage",
                            [_finding("seats", [("a", "A")]), _finding("more", [], known=False)], 0, False, T0 + DAY)
    assert after.problem and "a step has no answer" in after.note
    assert after.changes["seats"].compared and after.changes["more"].why == sched.NOT_KNOWN_NOW
    assert len(kept.runs("example.com", "license_usage")) == 1
    assert store.schedules("example.com")["license_usage"].done == T0


@pytest.mark.parametrize("meanwhile", ["unscheduled", "domain removed"])
def test_a_run_that_finishes_as_it_stops_being_scheduled_keeps_nothing(tmp_path, meanwhile):
    store, kept = _stores(tmp_path)
    store.put("example.com", "license_usage", "weekly", 2, T0)
    sched.after_run(store, kept, "example.com", "license_usage", [_finding("seats", [("a", "A")])], 0, True, T0)
    real = kept.runs

    def then_the_operator_acts(domain, run):
        found = real(domain, run)          # the compare has read what was kept: now Save or Remove lands
        if meanwhile == "unscheduled":
            assert sched.set_schedule(store, kept, domain, run, "", 0, T0 + 1) == (None, 1)
        else:
            assert sched.forget_domain(store, kept, domain) == (1, 1)
        return found
    kept.runs = then_the_operator_acts
    after = sched.after_run(store, kept, "example.com", "license_usage", [_finding("seats", [("a", "A")])], 0, True,
                            T0 + DAY)
    kept.runs = real
    assert after.note == "Not kept: it stopped being scheduled while it ran." and not after.problem
    assert list(kept.root.rglob("*.jsonl")) == [] and store.schedules("example.com") == {}


def test_drives_whose_members_weren_t_read_make_their_tables_partial_never_gone_or_unchanged():
    drives = [{"id": "A", "name": "Kept"}, {"id": "B", "name": "Dropped"}]
    acls = [{"id": "A", "name": "Kept", "permission": {"type": "user", "role": "organizer", "emailAddress": "a@example.com"}}]
    reads = [runs.StepRead(s, runs.READ) for s in (runs.DRIVES, runs.DRIVE_ACLS, runs.DOMAINS, runs.DIRECTORY)]
    perms, orphans, unread = runs.shared_drive_findings(drives, acls, ["example.com"],
                                                        [runs.GAMUser(primary_email="a@example.com")], frozenset(), reads)
    assert perms.partial and orphans.partial and not unread.partial
    assert "leaves out the 1 drive whose members weren't read" in perms.note
    whole = runs.shared_drive_findings(drives[:1], acls, ["example.com"],
                                       [runs.GAMUser(primary_email="a@example.com")], frozenset(), reads)
    assert not whole[0].partial and not whole[1].partial
    out = sched.changes_since([perms, orphans], 0, [], lambda i: sched.KeptRun(i))
    assert out["permissions"].why == out["orphaned"].why == sched.PART_UNKNOWN_NOW


def test_schedules_that_can_t_be_read_keep_nothing_and_say_so(tmp_path):
    store, kept = _stores(tmp_path)
    store.put("example.com", "license_usage", "weekly", 2, T0)
    os.chmod(store.path, 0)
    try:
        after = sched.after_run(store, kept, "example.com", "license_usage", [_finding("seats", [("a", "A")])], 0, True, T0)
    finally:
        os.chmod(store.path, 0o600)
    assert after.problem and "isn't kept and isn't compared" in after.note and after.changes == {}
    assert kept.runs("example.com", "license_usage") == []


def test_a_keep_the_disk_refuses_still_counts_and_says_it_wasn_t_kept(tmp_path, monkeypatch):
    store, kept = _stores(tmp_path)
    store.put("example.com", "license_usage", "weekly", 2, T0)

    def full(*_a, **_k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(kept, "keep", full)
    after = sched.after_run(store, kept, "example.com", "license_usage", [_finding("seats", [("a", "A")])], 0, True, T0)
    assert after.problem and "couldn't be kept on this Mac" in after.note
    assert store.schedules("example.com")["license_usage"].done == T0


# --- the screens --------------------------------------------------------------------------------------------------------

def _start(client, run, **form):  # noqa: F811
    r = client.post("/reports/runs/start", data={"run": run, **form})
    st = client.app.state.gamgui
    job = st.jobs.get(st.audit_run_jobs.get(run, ""))
    if job is not None:
        wait_for_job(client, job)
    return unescape(r.text), job


def _schedule(client, run="license_usage", cadence="weekly", keep="3"):  # noqa: F811
    return unescape(client.post("/reports/runs/schedule", data={"run": run, "cadence": cadence, "keep": keep}).text)


def _home(client):  # noqa: F811
    return unescape(client.get("/").text)


def _stores_of(client):  # noqa: F811
    st = client.app.state.gamgui
    return st.schedules, st.kept_runs


def test_a_scheduled_run_says_it_s_due_on_home_and_on_its_page_and_nothing_runs(client, gam_calls):  # noqa: F811
    before = len(gam_calls())
    assert "Scheduled audit runs" not in _home(client)
    panel = _schedule(client)
    assert "Saved: weekly, keeping the last 3 runs." in panel and 'data-announce="schedule-license_usage"' in panel
    assert "License usage is due. Weekly; no run of it has read every step since it was scheduled." in panel
    home = _home(client)
    assert 'href="/reports/runs#run-license_usage"' in home and ">License usage is due</a>" in home
    assert "Nothing runs on its own" in home
    page = unescape(client.get("/reports/runs").text)
    assert "License usage is due." in page and '<option value="weekly" selected>' in page and 'value="3"' in page
    assert len(gam_calls()) == before                  # saving, Home and the page ran nothing


def test_running_it_keeps_it_counts_it_and_records_its_reads_as_ever(client, gam_calls):  # noqa: F811
    _schedule(client)
    _, job = _start(client, "license_usage")
    st = client.app.state.gamgui
    result = st.audit_runs["license_usage"]
    assert result.answered and result.kept_note == "Kept on this Mac, as one of the last 3 runs it keeps."
    assert all(c.why == sched.NOTHING_BEFORE for c in result.changes.values())
    store, kept = _stores_of(client)
    assert len(kept.runs("example.com", "license_usage")) == 1 and store.schedules("example.com")["license_usage"].done
    records = [e for e in st.connector.audit.tail() if e["action"] == "read"]
    assert [r["extra"]["origin"] for r in records] == ["audit_run", "audit_run"]
    assert gam_writes(gam_calls()) == []
    home = _home(client)
    assert "Nothing is due." in home and "Not due yet:" in home
    assert re.search(r"License usage</a> \(weekly, next due [A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2} \d{4}\)", home)
    panel = unescape(client.get("/reports/runs").text)
    assert "Weekly, next due" in panel and "1 kept on this Mac" in panel and "Kept on this Mac, as one of the last 3" in panel


def test_the_next_run_says_what_changed_since_the_last(client, gam_state):  # noqa: F811
    _schedule(client)
    _start(client, "license_usage")
    (gam_state / "licenses_partial").touch()              # Google Vault's seat isn't read this time
    _start(client, "license_usage")
    result = client.app.state.gamgui.audit_runs["license_usage"]
    per_sku = result.changes["per_sku"]
    assert per_sku.compared and per_sku.new == [] and [r[1] for r in per_sku.gone] == ["Google Vault - Former Employee"]
    assert not result.changes["not_in_directory"].new and not result.changes["not_in_directory"].gone
    page = unescape(client.get("/reports/runs").text)
    assert ": 0 new, 1 gone." in page and "Gone since then (1)" in page and "No change since" in page
    assert "shows as gone and as new" in page
    # GAM gives no sign of a product it passed over, so a seat gone may be one it couldn't read: said beside each change
    assert page.count("a seat gone since then may be one it couldn't read this time") == 3


def test_a_drive_gam_dropped_is_never_gone_and_the_next_whole_run_compares_with_the_last_whole_one(client, gam_state):  # noqa: F811
    _schedule(client, run="shared_drive_permissions")
    _start(client, "shared_drive_permissions")
    st = client.app.state.gamgui
    first = st.schedules.schedules("example.com")["shared_drive_permissions"].done
    (gam_state / "acl_dropped").mkdir()
    (gam_state / "acl_dropped" / "0ALEGALuk9PVA").touch()       # Google refused one drive's members: no word, no code
    _start(client, "shared_drive_permissions")
    result = st.audit_runs["shared_drive_permissions"]
    assert result.answered and result.finding("unread").rows        # every step answered, one drive unread
    assert result.changes["permissions"].why == result.changes["orphaned"].why == sched.PART_UNKNOWN_NOW
    assert result.changes["unread"].compared and result.changes["unread"].new == [("Legal hold", "0ALEGALuk9PVA")]
    panel = unescape(client.get("/reports/runs").text).split('id="run-shared_drive_permissions"')[1].split("</section>")[0]
    members = panel.split('id="run-shared_drive_permissions-permissions"')[1].split('id="run-shared_drive_permissions-orphaned"')[0]
    assert "Part of this table is unknown this time" in members and "No change since" not in members and "gone." not in members
    (gam_state / "acl_dropped" / "0ALEGALuk9PVA").unlink()
    _start(client, "shared_drive_permissions")
    again = st.audit_runs["shared_drive_permissions"].changes["permissions"]
    assert again.compared and again.since == first and again.new == [] and again.gone == []   # the partial run passed over


def test_a_run_with_a_step_that_failed_is_not_kept_and_stays_due(client, gam_state):  # noqa: F811
    _schedule(client)
    (gam_state / "nolicensing").touch()
    _start(client, "license_usage")
    result = client.app.state.gamgui.audit_runs["license_usage"]
    assert not result.answered and result.kept_problem and "a step has no answer" in result.kept_note
    _, kept = _stores_of(client)
    assert kept.runs("example.com", "license_usage") == []
    assert ">License usage is due</a>" in _home(client)
    assert "No change since" not in unescape(client.get("/reports/runs").text)


def test_a_stopped_run_is_not_kept(client):  # noqa: F811
    _schedule(client)
    st = client.app.state.gamgui
    real = st.connector.audit_read

    async def then_stop(command, argv, key, **kw):
        rows = await real(command, argv, key, **kw)
        st.jobs[st.audit_run_jobs["license_usage"]].cancel_requested = True
        return rows
    st.connector.audit_read = then_stop
    _start(client, "license_usage")
    _, kept = _stores_of(client)
    assert kept.runs("example.com", "license_usage") == [] and ">License usage is due</a>" in _home(client)


def test_an_unscheduled_run_writes_nothing_to_disk(client):  # noqa: F811
    _start(client, "license_usage")
    assert client.app.state.gamgui.audit_runs["license_usage"].kept_note == ""
    assert not (Path(os.environ["HOME"]) / "Library" / "Application Support" / "GamGUI" / "schedules").exists()


def test_lowering_the_count_prunes_and_unscheduling_deletes_what_it_kept(client):  # noqa: F811
    _schedule(client, keep="3")
    for _ in range(3):
        _start(client, "license_usage")
    _, kept = _stores_of(client)
    assert len(kept.runs("example.com", "license_usage")) == 3
    assert "2 kept runs were deleted from this Mac." in _schedule(client, keep="1")
    assert len(kept.runs("example.com", "license_usage")) == 1
    panel = _schedule(client, cadence="")
    assert "Not scheduled. 1 kept run was deleted from this Mac." in panel
    assert kept.runs("example.com", "license_usage") == [] and "Scheduled audit runs" not in _home(client)


def test_delete_the_kept_runs_keeps_the_schedule(client):  # noqa: F811
    _schedule(client)
    _start(client, "license_usage")
    panel = unescape(client.post("/reports/runs/kept/delete", data={"run": "license_usage"}).text)
    assert "Deleted 1 kept run from this Mac." in panel and "Weekly, next due" in panel
    store, kept = _stores_of(client)
    assert kept.runs("example.com", "license_usage") == [] and "license_usage" in store.schedules("example.com")


@pytest.mark.parametrize("form, words", [
    ({"run": "license_usage", "cadence": "hourly", "keep": "3"}, "Pick daily, weekly, monthly or not scheduled."),
    ({"run": "license_usage", "cadence": "weekly", "keep": "0"}, "Keep a whole number of runs, from 1 to 12."),
    ({"run": "license_usage", "cadence": "weekly", "keep": "13"}, "Keep a whole number of runs, from 1 to 12."),
    ({"run": "license_usage", "cadence": "weekly", "keep": "2.5"}, "Keep a whole number of runs, from 1 to 12."),
    ({"run": "nope", "cadence": "weekly", "keep": "3"}, "That isn't one of the audit runs."),
])
def test_a_schedule_the_form_can_t_mean_is_refused_and_nothing_saved(client, form, words):  # noqa: F811
    assert words in unescape(client.post("/reports/runs/schedule", data=form).text)
    assert client.app.state.gamgui.schedules is None or client.app.state.gamgui.schedules.schedules("example.com") == {}


def test_a_blank_count_is_the_default(client):  # noqa: F811
    assert f"keeping the last {sched.KEEP_DEFAULT} runs" in _schedule(client, keep="")


def test_schedules_that_can_t_be_read_are_unknown_on_home_never_nothing_due(client):  # noqa: F811
    _schedule(client)
    store, _ = _stores_of(client)
    os.chmod(store.path, 0)
    try:
        home = _home(client)
        page = unescape(client.get("/reports/runs").text)
        refused = _schedule(client, cadence="daily")
    finally:
        os.chmod(store.path, 0o600)
    assert "couldn't be read just now, so what is due is unknown" in home and "Nothing is due." not in home
    assert "couldn't be read just now" in page and "couldn't be read just now" in refused
    assert store.schedules("example.com")["license_usage"].cadence == "weekly"      # never saved over


def test_a_schedules_file_that_isn_t_one_is_set_aside_and_home_says_where(client):  # noqa: F811
    store = sched.ScheduleStore()
    store.root.mkdir(parents=True)
    store.path.write_text("{not json")
    home = _home(client)
    assert "moved to" in home and ".unreadable-" in home and "Nothing is due." in home


def test_each_domain_has_its_own_schedules_and_kept_runs(client):  # noqa: F811
    _schedule(client)
    _start(client, "license_usage")
    st = client.app.state.gamgui
    home_com = st.connector
    st.activate(GAMConnector(runner=st.runner, domain="example.org", audit=home_com.audit))
    assert "Scheduled audit runs" not in _home(client)
    panel = unescape(client.get("/reports/runs").text).split('id="run-license_usage"')[1].split("</section>")[0]
    assert "Schedule: not scheduled</" in panel.replace("<span", "</") and "Kept on this Mac" not in panel
    assert "Weekly, next due" not in panel and "on example.org, here and on Home" in panel
    _schedule(client, run="classroom_courses", cadence="monthly")
    card = _home(client).split("Scheduled audit runs")[1].split("</section>")[0]
    assert ">Classroom is due</a>" in card and "License usage" not in card
    st.activate(home_com)
    card = _home(client).split("Scheduled audit runs")[1].split("</section>")[0]
    assert "License usage</a> (weekly, next due" in card and "Classroom" not in card
    panel = unescape(client.get("/reports/runs").text).split('id="run-license_usage"')[1].split("</section>")[0]
    assert "Kept on this Mac" in panel and "on example.com, here and on Home" in panel


def test_the_poll_of_a_running_run_reads_no_schedule(client):  # noqa: F811
    import asyncio
    _schedule(client)
    st = client.app.state.gamgui
    gate = asyncio.Event()
    real = st.connector.audit_read

    async def held(command, argv, key, **kw):
        await gate.wait()
        return await real(command, argv, key, **kw)
    st.connector.audit_read = held
    client.post("/reports/runs/start", data={"run": "license_usage"})
    job = st.jobs[st.audit_run_jobs["license_usage"]]
    store = st.schedules

    class Refuse:
        def __getattr__(self, name):
            raise AssertionError(f"the poll read the schedules ({name})")
    st.schedules = Refuse()
    assert "Step 1 of 2" in client.get(job.status_url).text
    st.schedules = store
    client.portal.call(gate.set)
    wait_for_job(client, job)


def test_the_kept_tables_live_under_the_app_s_data_folder_and_nowhere_else(client):  # noqa: F811
    _schedule(client)
    _start(client, "license_usage")
    root = Path(os.environ["HOME"]) / "Library" / "Application Support" / "GamGUI" / "schedules"
    if not root.exists():          # off macOS, app_data_dir is XDG's
        root = Path(os.environ["XDG_DATA_HOME"]) / "GamGUI" / "schedules"
    files = sorted(p.name for p in root.rglob("*") if p.is_file())
    assert files[-1] == "schedules.json" and len(files) == 2 and files[0].endswith(".jsonl")
    assert _mode(root) == 0o700 and all(_mode(p) == 0o600 for p in root.rglob("*") if p.is_file())


def test_removing_a_domain_on_setup_deletes_its_schedules_and_kept_runs(ctx):  # noqa: F811
    web, _, vault, state = ctx
    _store(vault, "example.com", "admin@example.com")
    _store(vault, "example.org", "boss@example.org")
    state.connector = GAMConnector(runner=state.runner, domain="example.com")
    store, kept = sched.ScheduleStore(), sched.KeptRuns()
    for domain in ("example.com", "example.org"):
        store.put(domain, "license_usage", "weekly", 3, T0)
        kept.keep(domain, "license_usage", T0, 0, [_finding("seats", [("a", "A")])])
    text = unescape(web.post("/setup/remove", data={"remove": "example.org"}).text)
    assert "Its 1 schedule and 1 kept run on this Mac were deleted." in text
    assert store.schedules("example.org") == {} and kept.runs("example.org", "license_usage") == []
    assert set(store.schedules("example.com")) == {"license_usage"} and len(kept.runs("example.com", "license_usage")) == 1


def test_removing_one_spelling_of_a_domain_still_listed_keeps_its_schedules(ctx):  # noqa: F811
    web, _, vault, state = ctx
    _store(vault, "Example.com", "admin@example.com")
    _store(vault, "example.com", "admin@example.com")
    _store(vault, "example.org", "boss@example.org")
    state.connector = GAMConnector(runner=state.runner, domain="example.org")
    store = sched.ScheduleStore()
    store.put("example.com", "license_usage", "weekly", 3, T0)
    text = unescape(web.post("/setup/remove", data={"remove": "Example.com"}).text)
    assert "Removed Example.com" in text and "were deleted" not in text
    assert set(store.schedules("example.com")) == {"license_usage"}
