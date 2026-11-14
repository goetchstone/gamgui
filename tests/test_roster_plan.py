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


def _cid(managed) -> str:
    """The SIS class ID a managed class's GamGUI alias carries (plain IDs only)."""
    return next(a for a in managed.aliases if a.startswith(f"d:{ALIAS_PREFIX}"))[len(f"d:{ALIAS_PREFIX}"):]


def _everyone(snap, classroom=()):
    """A directory in which every address the test uses is an account's primary, with no aliases."""
    found = {p.email for p in snap.people.values()} | {a for m in classroom for a in m.teachers + m.students}
    found |= {m.course.owner_email for m in classroom if m.course.owner_email}
    return {a: a for a in found}


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
    out = plan(snap, [current], _everyone(snap, [current]))
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
    handed = _kinds(plan(snap, [_managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com"])],
                         _everyone(snap)))
    assert handed == [("add_teacher", "ben@x.com"), ("owner", "ben@x.com"), ("remove_teacher", "ann@x.com")]


def test_no_teacher_is_removed_from_a_class_whose_owner_wasnt_read():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U2", "teacher", True)])
    current = _managed(alias_for("C1"), "500", "Biology", owner="", teachers=["ann@x.com"])
    out = plan(snap, [current], _everyone(snap, [current]))
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
    assert out.unmanaged_ignored == 1 and out.managed == 1         # an archived class isn't one GamGUI can act on


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
    out = plan(snap, [current], directory={"ann@x.com": "ann@x.com"})      # a stale directory that doesn't know cy
    assert out.changes == [] and "nor removed" in out.notes[0]


def test_a_class_whose_teachers_have_no_account_says_so_not_no_teacher():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True)])
    out = plan(snap, [], directory={})
    assert any("no teacher of it has a Google Workspace account" in n for n in out.notes)
    assert not any("no teacher, so" in n for n in out.notes)


def test_the_headline_never_says_it_matches_when_it_couldnt_tell():
    assert plan(_snap([], PEOPLE, []), [], unread=1).summary().startswith("Nothing could be planned")
    no_teacher = plan(_snap([("C1", "Biology")], PEOPLE, [("C1", "U3", "student", False)]), [])
    assert no_teacher.summary() == "Nothing GamGUI can plan; the notes below need attention."


def test_someone_without_a_workspace_account_is_noted_not_added():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True), ("C1", "U3", "student", False)])
    out = plan(snap, [], directory={"ann@x.com": "ann@x.com"})
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
    out = plan(snap, [_managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com"])],
               _everyone(snap))
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


# --- a SIS address that is another address of an account (review of the Swift port, 2026-11-14) ------------------------
# Classroom names people by their primary address. A SIS that still lists a renamed teacher's old address (now an alias)
# once read as "no account": the teacher was dropped from the class, another SIS teacher made owner, and she was removed.
ALIASED = {"jane.smith@x.com": "jane.smith@x.com", "jdoe@x.com": "jane.smith@x.com", "ben@x.com": "ben@x.com",
           "cy@x.com": "cy@x.com", "cy@alias.x.com": "cy@x.com"}


def _aliased_class():
    people = [("U1", "teacher", "jdoe@x.com"), ("U2", "teacher", "ben@x.com"), ("U3", "student", "cy@alias.x.com")]
    snap = _snap([("C1", "Biology")], people, [("C1", "U1", "teacher", True), ("C1", "U2", "teacher", False),
                                               ("C1", "U3", "student", False)])
    current = _managed(alias_for("C1"), "500", "Biology", owner="jane.smith@x.com",
                       teachers=["ben@x.com", "jane.smith@x.com"], students=["cy@x.com"])
    return snap, current


def test_a_teacher_listed_by_an_alias_keeps_the_class_she_owns():
    snap, current = _aliased_class()
    out = plan(snap, [current], ALIASED)
    assert out.changes == []                                  # no owner change (07), no removal (08, 10), no add
    assert any("2 addresses in the roster are another address" in n for n in out.notes)
    assert not any("no Google Workspace account" in n for n in out.notes)


def test_a_class_listed_by_an_alias_is_created_and_filled_with_primary_addresses():
    snap, _ = _aliased_class()
    out = plan(snap, [], ALIASED)
    assert _kinds(out) == [("create", "jane.smith@x.com"), ("add_teacher", "ben@x.com"), ("add_student", "cy@x.com")]


def test_without_the_directory_a_class_with_an_unmatched_address_keeps_its_people_and_owner():
    # No directory: jdoe@ may be jane.smith@ under another name, so nothing in that class is removed or handed on.
    snap, current = _aliased_class()
    out = plan(snap, [current])
    assert not [c for c in out.changes if c.kind in ("owner", "remove_teacher", "remove_student")]
    assert any("1 class lists roster addresses" in n for n in out.notes)


def test_without_the_directory_a_class_whose_addresses_all_match_is_still_planned():
    snap = _snap([("C1", "Biology")], PEOPLE, [("C1", "U1", "teacher", True), ("C1", "U3", "student", False)])
    current = _managed(alias_for("C1"), "500", "Biology", owner="ann@x.com", teachers=["ann@x.com", "ben@x.com"],
                       students=["cy@x.com", "di@x.com"])
    out = plan(snap, [current])
    assert _kinds(out) == [("remove_teacher", "ben@x.com"), ("remove_student", "di@x.com")]


def test_a_set_of_addresses_is_refused_as_a_directory():
    # A set can't say which account an alias belongs to: refused, never read as a directory without aliases.
    import pytest
    snap, current = _aliased_class()
    with pytest.raises(TypeError):
        plan(snap, [current], {"jane.smith@x.com", "ben@x.com"})


def test_the_directory_maps_every_address_gam_prints_to_its_primary():
    # `print users fields …,aliases,… formatjson` asks for aliases *and* nonEditableAliases (USER_FIELDS_CHOICE_MAP,
    # read from the 7.52.01 build) and dumps each as a JSON list of strings; an account without them has no such key.
    from gamgui.core.gam.models import GAMUser
    from gamgui.core.roster_plan import directory_from
    users = [GAMUser.from_json({"primaryEmail": "Jane.Smith@x.com", "aliases": ["JDoe@x.com"],
                                "nonEditableAliases": ["jane.smith@alias.x.com"]}),
             GAMUser.from_json({"primaryEmail": "ben@x.com"})]
    assert directory_from(users) == {"jane.smith@x.com": "jane.smith@x.com", "jdoe@x.com": "jane.smith@x.com",
                                     "jane.smith@alias.x.com": "jane.smith@x.com", "ben@x.com": "ben@x.com"}


# --- the risk check's bases (review of the Swift port, 2026-11-14) -------------------------------------------------------
def test_removing_every_co_teacher_is_flagged_against_the_teachers_not_everyone():
    # 30 classes, each an owner, a co-teacher and 30 students: the co-teachers are half the teachers, 3% of everyone.
    n = LIMITS["remove_teacher"][0] + 5
    classes = [(f"C{i}", f"Class {i}") for i in range(n)]
    snap = _snap(classes, PEOPLE, [(cid, "U1", "teacher", True) for cid, _ in classes])
    current = [_managed(alias_for(cid), f"5{i:02}", title, owner="ann@x.com", teachers=["ann@x.com", "ben@x.com"],
                        students=[f"s{j}@x.com" for j in range(30)]) for i, (cid, title) in enumerate(classes)]
    for c in current:      # the students stay: the SIS has them too
        for s in c.students:
            snap.people[s] = snap.people.get(s) or type(snap.people["U3"])(s, "student", s)
            snap.enrollments.append(type(snap.enrollments[0])(_cid(c), s, "student", False))
    out = plan(snap, current, _everyone(snap, current))
    assert out.counts == {"remove_teacher": n} and out.teachers == 2 * n
    assert "remove_teacher" in out.flags and "of the teachers in them" in out.flags["remove_teacher"]


def test_archived_classes_dont_dilute_the_risk_bases():
    # Archived classes can't be archived or removed from, so they never count in what a removal or archive is measured by.
    n = LIMITS["archive"][0] + 5
    gone = [_managed(alias_for(f"OLD{i}"), f"7{i:02}", f"Old {i}", owner="ann@x.com", teachers=["ann@x.com"])
            for i in range(n)]
    archived = [_managed(alias_for(f"PAST{i}"), f"8{i:03}", f"Past {i}", owner="ann@x.com", teachers=["ann@x.com"],
                         students=["cy@x.com", "di@x.com"], state="ARCHIVED") for i in range(10 * n)]
    out = plan(_snap([], PEOPLE, []), gone + archived, _everyone(_snap([], PEOPLE, []), gone + archived))
    assert out.counts == {"archive": n} and out.managed == n and out.enrolled == n and out.teachers == n
    assert "archive" in out.flags
