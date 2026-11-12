"""Hand the change list over as files (plan 2026-11-11 C4). Pure: no GAM. The bytes are a fixture SwiftGamGui matches:
regenerate it with HANDOVER_UPDATE=1 only when the change is meant."""
from __future__ import annotations

import csv
import functools
import io
import os
import re
from pathlib import Path

import pytest

from gamgui.core.roster_handover import STEPS, HandoverError, build, gam_line
from gamgui.core.roster_plan import Change, ChangeList

FIXTURE = Path(__file__).parent / "fixtures" / "handover"
GRAMMAR = Path(__file__).parent.parent / "gamgui" / "resources" / "gam7" / "GamCommands.txt"


def _plan(**extra) -> ChangeList:
    """One of every kind, with the awkward values a district has: a quote, a comma, a formula lead, no section."""
    changes = [
        Change("remove_teacher", "d:gg-C2", "Chemistry", person="cy@x.com", swap=True),
        Change("remove_student", "d:gg-C2", "Chemistry", person="di@x.com", swap=True),
        Change("create", "d:gg-C3", 'Physics, "Lab"', person="ann@x.com", after='Physics, "Lab" · Fall 2026',
               name='Physics, "Lab"', section="Fall 2026"),
        Change("create", "d:gg-C4", "=Study hall", person="ann@x.com", after="=Study hall", name="=Study hall"),
        Change("rename", "d:gg-C1", "Biology", before="Bio · Fall", after="Biology · Fall 2026", name="Biology",
               section="Fall 2026", course_id="500"),
        Change("add_teacher", "d:gg-C3", 'Physics, "Lab"', person="ben@x.com"),
        Change("add_student", "d:gg-C3", 'Physics, "Lab"', person="cy@x.com"),
        Change("owner", "d:gg-C1", "Biology", person="ben@x.com", before="ann@x.com", after="ben@x.com", course_id="500"),
        Change("remove_teacher", "d:gg-C1", "Biology", person="ann@x.com", course_id="500"),
        Change("add_student", "d:gg-C1", "Biology", person="ann@x.com", course_id="500", late=True),
        Change("rename", "d:gg-C5", "Art", before="Art · Spring", after="Art", name="Art", course_id="501"),
        Change("remove_student", "d:gg-C1", "Biology", person="ed@x.com", course_id="500"),
        Change("archive", "d:gg-OLD", "Old class", before="Active", course_id="700"),
    ]
    out = ChangeList(changes=changes)
    for c in changes:
        out.counts[c.kind] = out.counts.get(c.kind, 0) + 1
    for key, value in extra.items():
        setattr(out, key, value)
    return out


def test_the_files_match_the_fixture_byte_for_byte():
    files = build(_plan(), "example.com", "2026-11-11 18:00 UTC")
    if os.environ.get("HANDOVER_UPDATE"):
        FIXTURE.mkdir(parents=True, exist_ok=True)
        for old in FIXTURE.iterdir():
            old.unlink()
        for name, body in files.items():
            (FIXTURE / name).write_bytes(body)
    assert sorted(files) == sorted(p.name for p in FIXTURE.iterdir())
    for name, body in files.items():
        assert body == (FIXTURE / name).read_bytes(), name


def test_every_step_runs_in_order_and_names_a_class_by_its_alias():
    files = build(_plan(), "example.com", "now")
    names = [s.file for s in STEPS if s.file in files]
    numbers = [int(n[:2]) for n in names]
    assert numbers == sorted(numbers)                                     # numbered in run order
    assert names[0].startswith("01-remove-teachers-changing-role")       # a swap's removals before any add
    readme = files["README.txt"].decode()
    assert readme.index("01-remove-teachers") < readme.index("05-add-teachers") < readme.index("07-change-owners") < readme.index("08-remove-teachers") < readme.index("09-add-former-owners") < readme.index("11-archive")
    assert "they don't pass its preview" in readme and "Never open the numbered files in a spreadsheet" in readme


def test_the_review_sheet_is_formula_safe_and_the_gam_files_are_exact():
    files = build(_plan(), "example.com", "now")
    review = list(csv.reader(io.StringIO(files["review.csv"].decode())))
    assert ["Classes to create", "'=Study hall"] == review[[r[2] for r in review].index("d:gg-C4")][:2]
    create = list(csv.reader(io.StringIO(files["03-create-classes-without-section.csv"].decode())))
    assert create == [["alias", "name", "owner"], ["d:gg-C4", "=Study hall", "ann@x.com"]]
    sectioned = list(csv.reader(io.StringIO(files["03-create-classes.csv"].decode())))
    assert sectioned[1] == ["d:gg-C3", 'Physics, "Lab"', "Fall 2026", "ann@x.com"]
    assert b"\\" not in files["03-create-classes.csv"]                   # plain CSV: quotes doubled, no escape character


def test_each_command_is_one_the_grammar_has():
    if not GRAMMAR.exists():
        pytest.skip("the GAM grammar isn't vendored")
    grammar = GRAMMAR.read_text(errors="replace")
    words = set(re.findall(r"[\w-]+", grammar))
    heads = [line.split()[1:] for line in grammar.splitlines() if line.startswith("gam ")]
    assert any(h[:2] == ["csv", "<CSVLoopContent>"] for h in heads)
    for step in STEPS:
        line = gam_line(step)
        assert "noescapechar true" in line and line.startswith(f"gam csv {step.file} noescapechar true gam ")
        argv = list(step.gam)
        for word in argv:
            assert word.startswith("~") or word in words, (step.file, word)
        assert any(_lines_up(argv, head) for head in heads), (step.file, argv)
        assert {w[1:] for w in argv if w.startswith("~")} <= set(step.columns), step.file


@functools.lru_cache(maxsize=None)
def _definitions() -> dict:
    """Each ``<Nonterminal> ::= …`` and the words its definition (with its continuation lines) holds."""
    defs, current = {}, None
    for line in GRAMMAR.read_text(errors="replace").splitlines():
        found = re.match(r"(<[\w-]+>) ::=(.*)", line)
        if found:
            current = found.group(1)
            defs[current] = set(re.findall(r"[\w-]+", found.group(2)))
        elif current and line.startswith((" ", "\t")):
            defs[current] |= set(re.findall(r"[\w-]+", line))
        else:
            current = None
    return defs


def _lines_up(argv, head, defs=None) -> bool:
    """The first four words line up with a grammar line: a word is one of its choices, a ``~field`` sits in a ``<slot>``,
    and a keyword in a ``<Nonterminal>`` slot is one that nonterminal's definition holds (`name` in <CourseAttribute>)."""
    defs = _definitions() if defs is None else defs
    if len(head) < min(4, len(argv)):
        return False
    for word, slot in zip(argv[:4], head[:4], strict=False):   # a grammar line may be shorter: checked above
        bare = slot.strip("[]()+*")
        if word.startswith("~"):
            if "<" not in slot:
                return False
        elif word not in bare.split("|") and word not in defs.get(bare, set()):
            return False
    return True


def test_a_flagged_kind_is_left_out_unless_ticked():
    plan = _plan(flags={"remove_student": "Would remove a lot."})
    left = build(plan, "example.com", "now")
    assert "10-remove-students.csv" not in left and "02-remove-students-changing-role.csv" not in left
    assert "Held back by the risk check" in left["README.txt"].decode()
    ticked = build(plan, "example.com", "now", include_flagged=["remove_student"])
    assert "10-remove-students.csv" in ticked


def test_what_needs_a_held_back_removal_is_left_out_with_it():
    # di@ swaps student -> teacher in Chemistry: with the removal held back, adding di@ as a teacher would fail.
    plan = _plan(flags={"remove_student": "Would remove a lot."})
    plan.changes.append(Change("add_teacher", "d:gg-C2", "Chemistry", person="di@x.com"))
    files = build(plan, "example.com", "now")
    teachers = list(csv.reader(io.StringIO(files["05-add-teachers.csv"].decode())))
    assert ["d:gg-C2", "di@x.com"] not in teachers and "Also left out: 1 change that needs" in files["README.txt"].decode()
    # And an outgoing owner's student add goes with their held-back removal.
    held = build(_plan(flags={"remove_teacher": "Would remove a lot."}), "example.com", "now")
    assert "09-add-former-owners-as-students.csv" not in held and "08-remove-teachers.csv" not in held


def test_a_rename_always_sends_its_section_so_an_empty_one_clears_it():
    files = build(_plan(), "example.com", "now")
    rows = list(csv.reader(io.StringIO(files["04-rename-classes.csv"].decode())))
    assert rows[0] == ["course", "name", "section"] and ["d:gg-C5", "Art", ""] in rows
    assert not any(n.startswith("04-") and n != "04-rename-classes.csv" for n in files)


@pytest.mark.skipif(not Path("/bin/zsh").exists(), reason="no zsh")
def test_each_readme_line_runs_as_written_in_zsh():
    # zsh reads an unquoted leading ~word as a home folder and refuses the line (review of C4).
    import subprocess
    for step in STEPS:
        out = subprocess.run(["/bin/zsh", "-fc", "print -rl -- " + gam_line(step)], capture_output=True, text=True,
                             check=True).stdout.splitlines()
        assert out == ["gam", "csv", step.file, "noescapechar", "true", "gam", *step.gam], step.file


def test_an_incomplete_list_or_nothing_to_do_hands_nothing_over():
    with pytest.raises(HandoverError, match="incomplete"):
        build(_plan(incomplete=True), "example.com", "now")
    with pytest.raises(HandoverError, match="nothing to hand over"):
        build(ChangeList(), "example.com", "now")
