"""The change list (plan 2026-11-11 C3): what would make Classroom match a kept SIS roster. Pure: no GAM."""
from __future__ import annotations

from gamgui.core.classroom import Course, Participant, Roster
from gamgui.core.roster import Class, Enrollment, Person, Snapshot, Term
from gamgui.core.roster_plan import ALIAS_PREFIX, LIMITS, Managed, alias_for, plan


def _snap(classes, people, enrollments) -> Snapshot:
    snap = Snapshot(source="test")
    snap.terms = {"T1": Term("T1", "Fall 2026")}
    snap.classes = {cid: Class(cid, title, "CO1", "S1", ("T1",)) for cid, title in classes}
    snap.people = {pid: Person(pid, role, email) for pid, role, email in people}
    snap.enrollments = [Enrollment(cid, pid, role, primary) for cid, pid, role, primary in enrollments]
    return snap


PEOPLE = [("U1", "teacher", "ann@x.com"), ("U2", "teacher", "ben@x.com"), ("U3", "student", "cy@x.com"),
          ("U4", "student", "di@x.com")]


def _managed(alias, cid, name, owner, teachers=(), students=(), state="ACTIVE", section="Fall 2026") -> Managed:
    course = Course(id=cid, name=name, section=section, state=state, owner_email=owner)
    roster = Roster(cid, name, teachers=[Participant("TEACHER", "", t) for t in teachers],
                    students=[Participant("STUDENT", "", s) for s in students])
    return Managed.from_read(course, [alias] if alias else [], roster)


def _kinds(out):
    return [(c.kind, c.person) for c in out.changes]


def test_an_alias_is_the_sis_id_when_plain_and_a_hash_otherwise():
    assert alias_for("C-101.a_b") == f"d:{ALIAS_PREFIX}C-101.a_b"
    weird = alias_for("class #1 / ünï")
    assert weird.startswith(f"d:{ALIAS_PREFIX}h") and len(weird) == len(f"d:{ALIAS_PREFIX}h") + 24
    assert alias_for("class #1 / ünï") == weird


def test_a_new_class_is_created_owned_by_its_primary_teacher_then_filled():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", False), ("C1", "U2", "teacher", True),
                                               ("C1", "U3", "student", False)])
    out = plan(snap, [])
    assert _kinds(out) == [("create", "ben@x.com"), ("add_teacher", "ann@x.com"), ("add_student", "cy@x.com")]
    assert out.changes[0].alias == alias_for("C1") and out.changes[0].after == "Biology · Fall 2026"


def test_an_existing_class_is_renamed_filled_emptied_and_its_owner_changed_in_order():
    snap = _snap([("C1", "Biology II")], PEOPLE, [("C1", "U2", "teacher", True), ("C1", "U3", "student", False)])
    current = _managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com"],
                       students=["di@x.com"])
    out = plan(snap, [current])
    assert _kinds(out) == [("rename", ""), ("add_teacher", "ben@x.com"), ("add_student", "cy@x.com"),
                           ("owner", "ben@x.com"), ("remove_teacher", "ann@x.com"), ("remove_student", "di@x.com")]
    assert all(c.course_id == "500" for c in out.changes)
    owner = next(c for c in out.changes if c.kind == "owner")
    assert (owner.before, owner.after) == ("ann@x.com", "ben@x.com")


def test_the_owner_stays_unless_handed_on_and_then_goes_after_the_owner_change():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True)])
    kept = _managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com", "ben@x.com"])
    assert _kinds(plan(snap, [kept])) == [("remove_teacher", "ben@x.com")]          # ann owns it and the SIS agrees
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U2", "teacher", True)])
    handed = _kinds(plan(snap, [_managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com"])]))
    assert handed == [("add_teacher", "ben@x.com"), ("owner", "ben@x.com"), ("remove_teacher", "ann@x.com")]


def test_no_teacher_is_removed_from_a_class_whose_owner_wasnt_read():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U2", "teacher", True)])
    current = _managed(alias_for("C1"), "500", "Biology", owner="", teachers=["ann@x.com"])
    out = plan(snap, [current])
    assert _kinds(out) == [("add_teacher", "ben@x.com")] and "owner couldn't be read" in out.notes[0]


def test_a_role_swap_removes_before_it_adds():
    # Classroom refuses to add someone already in the class: add-then-remove would leave both out of it.
    snap = _snap([("C1", "Biology")], PEOPLE + [("U5", "student", "ann2@x.com")],
                 [("C1", "U1", "teacher", True), ("C1", "U4", "teacher", False), ("C1", "U3", "student", False)])
    people = {p.id: p for p in snap.people.values()}
    people["U4"] = people["U4"].__class__("U4", "teacher", "di@x.com")
    snap.people = people
    current = _managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com", "cy@x.com"],
                       students=["di@x.com"])
    kinds = _kinds(plan(snap, [current]))
    assert kinds.index(("remove_teacher", "cy@x.com")) < kinds.index(("add_student", "cy@x.com"))
    assert kinds.index(("remove_student", "di@x.com")) < kinds.index(("add_teacher", "di@x.com"))


def test_only_classes_with_a_gamgui_alias_are_compared_or_archived():
    snap = _snap([], PEOPLE, [])
    handmade = _managed("d:something-else", "600", "Art club", owner="ann@x.com")
    gone = _managed(alias_for("OLD"), "700", "Old class", owner="ann@x.com")
    archived = _managed(alias_for("OLDER"), "800", "Older", owner="ann@x.com", state="ARCHIVED")
    out = plan(snap, [handmade, gone, archived])
    assert _kinds(out) == [("archive", "")] and out.changes[0].course_id == "700"
    assert out.unmanaged_ignored == 1 and out.managed == 2


def test_an_alias_printed_without_its_scope_is_still_ours():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True)])
    current = _managed(alias_for("C1")[2:], "500", "Biology", owner="ann@x.com", teachers=["ann@x.com"])
    assert plan(snap, [current]).changes == []


def test_a_class_with_no_teacher_is_noted_not_created():
    out = plan(_snap([("C1", "Biology")], PEOPLE, [("C1", "U3", "student", False)]), [])
    assert out.changes == [] and "no teacher" in out.notes[0]


def test_aliases_are_read_as_gam_prints_them_objects_and_all():
    from gamgui.core.roster_plan import managed_from
    course = {"id": "9", "name": "X"}
    people = [{"courseId": "9", "courseName": "X", "JSON-teachers": "[]", "JSON-students": "[]"}]
    classes, unread = managed_from([{**course, "JSON-aliases": '[{"alias": "d:gg-C9"}, {"alias": "d:other"}]'}], people)
    assert classes[0].aliases == ("d:gg-C9", "d:other") and unread == []
    classes, unread = managed_from([{**course, "JSON-aliases": '[{"etag": "x"}]'}], people)
    assert classes == [] and unread == ["9"]                     # an item it can't read: unread, never an alias


def test_a_class_that_looks_like_one_already_there_is_never_created_twice():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True)])
    lookalike = _managed(None, "600", "Biology", owner="ann@x.com")   # GAM printed [] for it: its alias read may have failed
    out = plan(snap, [lookalike])
    assert out.changes == [] and "1 SIS class matches a Classroom class by name and section" in out.notes[0]


def test_the_directory_never_causes_a_removal():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True), ("C1", "U3", "student", False)])
    current = _managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com"], students=["cy@x.com"])
    out = plan(snap, [current], directory={"ann@x.com"})      # a stale directory that doesn't know cy
    assert out.changes == [] and "nor removed" in out.notes[0]


def test_a_class_whose_teachers_have_no_account_says_so_not_no_teacher():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True)])
    out = plan(snap, [], directory=set())
    assert any("no teacher of it has a Google Workspace account" in n for n in out.notes)
    assert not any("no teacher, so" in n for n in out.notes)


def test_the_headline_never_says_it_matches_when_it_couldnt_tell():
    assert plan(_snap([], PEOPLE, []), [], unread=1).summary().startswith("Nothing could be planned")
    no_teacher = plan(_snap([("C1", "Biology")], PEOPLE, [("C1", "U3", "student", False)]), [])
    assert no_teacher.summary() == "Nothing GamGUI can plan; the notes below need attention."


def test_someone_without_a_workspace_account_is_noted_not_added():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True), ("C1", "U3", "student", False)])
    out = plan(snap, [], directory={"ann@x.com"})
    assert _kinds(out) == [("create", "ann@x.com")] and "no Google Workspace account" in out.notes[0]


def test_a_teacher_listed_as_a_student_too_is_only_a_teacher():
    people = PEOPLE + [("U5", "student", "ann@x.com")]
    snap = _snap([("C1", "Biology")], people, [("C1", "U1", "teacher", True), ("C1", "U5", "student", False)])
    assert _kinds(plan(snap, [])) == [("create", "ann@x.com")]


def test_mass_removals_and_archives_are_flagged_past_both_limits():
    count_limit, share = LIMITS["remove_student"]
    students = [f"s{i}@x.com" for i in range(count_limit + 1)]
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True)])
    current = _managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com"], students=students)
    out = plan(snap, [current])
    assert out.counts["remove_student"] == count_limit + 1
    assert "remove_student" in out.flags and "Would remove" in out.flags["remove_student"]


def test_a_few_removals_are_not_flagged():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True)])
    current = _managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com"], students=["cy@x.com"])
    assert plan(snap, [current]).flags == {}


def test_a_class_the_sis_wants_that_classroom_archived_is_noted_not_planned():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True)])
    archived = _managed(alias_for("C1"), "500", "Bio", owner="ann@x.com", state="ARCHIVED")
    out = plan(snap, [archived])
    assert out.changes == [] and "archived in Classroom" in out.notes[0]


def test_a_class_that_couldnt_be_read_makes_the_list_incomplete():
    out = plan(_snap([], PEOPLE, []), [], unread=2)
    assert out.incomplete and "couldn't be read whole" in out.notes[0]


# --- the state read, through the strict mock -----------------------------------------------------------------------------
async def test_classroom_state_reads_every_class_with_its_aliases_and_people(connector, gam_calls):
    classes, unread = await connector.classroom_state()
    assert unread == []
    by_id = {m.course.id: m for m in classes}
    assert by_id["500000000002"].aliases == ("d:gg-C2", "d:chem-honors")
    assert by_id["500000000002"].students == ("bob@example.com", "carol@example.com")
    assert by_id["500000000003"].aliases == () and by_id["500000000004"].course.state == "ARCHIVED"
    runs = [a for a in gam_calls() if a[:2] in (["print", "courses"], ["print", "course-participants"])]
    assert len(runs) == 2                                 # two GAM runs, however many classes


async def test_the_mock_prints_aliases_as_objects_as_the_build_does(runner, domain):
    # doPrintCourses' formatjson branch dumps courses.aliases.list's items raw: objects, not strings (7.51.00 build).
    import json

    from gamgui.core.gam.commands import GAMCommands
    from gamgui.core.gam.parser import parse_records
    out = await runner.run_authenticated(domain, GAMCommands.print_courses(states=["active"], aliases=True))
    cells = [json.loads(r["JSON-aliases"]) for r in parse_records(out)]
    assert any(cells) and all(isinstance(item, dict) and set(item) == {"alias"} for cell in cells for item in cell)


def test_a_roster_row_missing_for_a_class_is_unread_never_empty():
    from gamgui.core.roster_plan import managed_from
    course = {"id": "9", "name": "X", "JSON-aliases": '["d:gg-C9"]'}
    classes, unread = managed_from([course], [])
    assert classes == [] and unread == ["9"]
    classes, unread = managed_from([{**course, "JSON-aliases": "{oops"}],
                                   [{"courseId": "9", "courseName": "X", "JSON-teachers": "[]", "JSON-students": "[]"}])
    assert unread == ["9"]


def test_the_summary_says_destructive_parts_first():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True), ("C1", "U3", "student", False)])
    gone = _managed(alias_for("OLD"), "700", "Old", owner="ann@x.com")
    out = plan(snap, [gone])
    assert out.summary() == "Would archive 1 class; would create 1 class and add 1 person."
    assert plan(_snap([], PEOPLE, []), []).summary() == "Classroom already matches the roster: nothing to change."


def test_an_outgoing_owner_who_becomes_a_student_is_moved_after_the_owner_change():
    # Classroom won't remove a class's owner: the removal and the student add wait for the owner change (review of C4).
    snap = _snap([("C1", "Biology")], PEOPLE + [("U9", "student", "ann@x.com")],
                 [("C1", "U2", "teacher", True), ("C1", "U9", "student", False)])
    out = plan(snap, [_managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com"])])
    assert _kinds(out) == [("add_teacher", "ben@x.com"), ("owner", "ben@x.com"), ("remove_teacher", "ann@x.com"),
                           ("add_student", "ann@x.com")]
    removal, add = out.changes[2], out.changes[3]
    assert not removal.swap and add.late


def test_a_student_who_stays_a_teacher_here_is_noted_not_added():
    # The owner can't be removed while the SIS has no teacher with an account to hand the class to.
    snap = _snap([("C1", "Biology")], [("U9", "student", "ann@x.com")], [("C1", "U9", "student", False)])
    out = plan(snap, [_managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com"])])
    assert _kinds(out) == [] and any("still a teacher in Classroom" in n for n in out.notes)


def test_a_rename_that_drops_the_section_carries_an_empty_one():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True)])
    snap.classes["C1"] = snap.classes["C1"].__class__("C1", "Biology", "CO1", "S1", ())
    out = plan(snap, [_managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com"])])
    assert [(c.kind, c.name, c.section, c.after) for c in out.changes] == [("rename", "Biology", "", "Biology")]
