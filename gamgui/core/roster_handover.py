"""Hand the Classroom change list over as files (plan 2026-11-11, C4).

GamGUI never writes to Classroom. What it hands over is for someone to run with GAM, outside this app: a review sheet for
people, and one CSV per kind of change with the `gam csv` line that runs it, numbered in the order they must run. Pure: a
`ChangeList` in, files out. Nothing here runs GAM.

- **The review sheet** (`review.csv`) is meant to be opened in a spreadsheet, so every cell is formula-safe (`csv_safe`).
- **The GAM files keep their values exact** (a leading ``'`` would change a class's name), so they must never be opened and
  saved in a spreadsheet; the README says so. Each is plain CSV (doubled quotes, no escape character) and each command says
  `noescapechar true`: GAM reads its CSVs with a backslash escape by default, which would misread a name holding one.
- **Every value is a whole argument.** `gam csv` replaces an argument that is exactly ``~field`` with that field (grammar
  1430), so a value reaches GAM as one argument and is never split. No ``~~field~~`` inside a larger argument.
- **A class is named by its GamGUI alias** (`<CourseID> ::= <Number>|d:<CourseAlias>`), so a class created in step one is
  found by later steps without its numeric ID.
- **Run order:** a role swap's removals first (Classroom refuses to add someone already in a class), then create, rename,
  add teachers, add students, owners, remove teachers, add a former owner as a student (Classroom won't remove an owner, so
  that removal waits for the owner change), remove students, archive. A flagged kind (C3's risk check) is left out unless
  the operator ticked it, and so is what depends on it; an incomplete list hands nothing over.
- **The README's lines are shell-quoted** (`shlex.join`): an unquoted ``~field`` is a home-folder lookup in zsh.

The bytes are fixed by `tests/fixtures/handover/`, which SwiftGamGui's port matches byte for byte.
"""
from __future__ import annotations

import csv
import io
import shlex
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Sequence, Tuple

from .csv_safe import csv_safe
from .roster_plan import KINDS, Change, ChangeList

# Each GAM file: its name, which changes it holds, its columns and values, and the GAM line after `gam csv <file> …`.
_Row = Callable[[Change], Tuple[str, ...]]


@dataclass(frozen=True)
class Step:
    file: str
    what: str
    pick: Callable[[Change], bool]
    columns: Tuple[str, ...]
    row: _Row
    gam: Tuple[str, ...]


STEPS: Tuple[Step, ...] = (
    Step("01-remove-teachers-changing-role.csv", "Remove teachers who become students in the same class",
         lambda c: c.kind == "remove_teacher" and c.swap, ("course", "email"), lambda c: (c.alias, c.person),
         ("course", "~course", "remove", "teachers", "~email")),
    Step("02-remove-students-changing-role.csv", "Remove students who become teachers in the same class",
         lambda c: c.kind == "remove_student" and c.swap, ("course", "email"), lambda c: (c.alias, c.person),
         ("course", "~course", "remove", "students", "~email")),
    Step("03-create-classes.csv", "Create classes, active, owned by their primary teacher",
         lambda c: c.kind == "create" and bool(c.section), ("alias", "name", "section", "owner"),
         lambda c: (c.alias, c.name, c.section, c.person),
         ("create", "course", "alias", "~alias", "name", "~name", "section", "~section", "teacher", "~owner", "state", "active")),
    Step("03-create-classes-without-section.csv", "Create classes that have no section",
         lambda c: c.kind == "create" and not c.section, ("alias", "name", "owner"),
         lambda c: (c.alias, c.name, c.person),
         ("create", "course", "alias", "~alias", "name", "~name", "teacher", "~owner", "state", "active")),
    # Always with its section: an empty one clears it (GAM reads `section` with no minimum length), so a class whose term
    # went from the SIS loses it in Classroom too, instead of being renamed again on every compare.
    Step("04-rename-classes.csv", "Rename classes; an empty section clears one", lambda c: c.kind == "rename",
         ("course", "name", "section"), lambda c: (c.alias, c.name, c.section),
         ("update", "course", "~course", "name", "~name", "section", "~section")),
    Step("05-add-teachers.csv", "Add teachers", lambda c: c.kind == "add_teacher", ("course", "email"),
         lambda c: (c.alias, c.person), ("course", "~course", "add", "teachers", "~email")),
    Step("06-add-students.csv", "Add students", lambda c: c.kind == "add_student" and not c.late, ("course", "email"),
         lambda c: (c.alias, c.person), ("course", "~course", "add", "students", "~email")),
    Step("07-change-owners.csv", "Change owners; each new owner is already a teacher of the class",
         lambda c: c.kind == "owner", ("course", "owner"), lambda c: (c.alias, c.person),
         ("update", "course", "~course", "owner", "~owner")),
    Step("08-remove-teachers.csv", "Remove teachers, a former owner once step 7 has run",
         lambda c: c.kind == "remove_teacher" and not c.swap, ("course", "email"),
         lambda c: (c.alias, c.person), ("course", "~course", "remove", "teachers", "~email")),
    Step("09-add-former-owners-as-students.csv", "Add as students the former owners removed in step 8",
         lambda c: c.kind == "add_student" and c.late, ("course", "email"),
         lambda c: (c.alias, c.person), ("course", "~course", "add", "students", "~email")),
    Step("10-remove-students.csv", "Remove students", lambda c: c.kind == "remove_student" and not c.swap, ("course", "email"),
         lambda c: (c.alias, c.person), ("course", "~course", "remove", "students", "~email")),
    Step("11-archive-classes.csv", "Archive classes", lambda c: c.kind == "archive", ("course",), lambda c: (c.alias,),
         ("update", "course", "~course", "state", "archived")),
)


class HandoverError(Exception):
    """The change list can't be handed over. ``str()`` says why."""


def _plain_csv(rows: Iterable[Sequence[str]]) -> bytes:
    """CSV as GAM reads it with `noescapechar true`: comma, double quote, quotes doubled, no escape character, CRLF."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL, doublequote=True)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def gam_line(step: Step) -> str:
    """The command that runs a step's file, as typed in a terminal. ``shlex.quote`` single-quotes each ``~field``: zsh (and
    bash, when a user of that name exists) reads an unquoted leading ``~word`` as a home folder."""
    return shlex.join(["gam", "csv", step.file, "noescapechar", "true", "gam", *step.gam])


def _split(plan: ChangeList, include_flagged: Iterable[str]) -> Tuple[List[Change], int]:
    """(the changes handed over, how many were left out because they depend on a held-back one). A flagged kind not ticked
    is left out, and so is what needs it: an add or owner change of someone whose removal from that class is held back
    (they're still in it, so the add fails), and the removal and student add of an outgoing owner whose change is."""
    ticked = set(include_flagged)
    held = [c for c in plan.changes if c.kind in plan.flags and c.kind not in ticked]
    blocked = {(c.alias, c.person) for c in held if c.kind in ("remove_teacher", "remove_student")}
    blocked |= {(c.alias, c.before) for c in held if c.kind == "owner"}
    kept, dependent = [], 0
    for c in plan.changes:
        if c.kind in plan.flags and c.kind not in ticked:
            continue
        if (c.alias, c.person) in blocked and c.kind in ("add_teacher", "add_student", "owner", "remove_teacher"):
            dependent += 1
            continue
        kept.append(c)
    return kept, dependent


def kept_changes(plan: ChangeList, include_flagged: Iterable[str] = ()) -> List[Change]:
    """The changes handed over: all of them but a flagged kind not ticked, and what depends on one (``_split``)."""
    return _split(plan, include_flagged)[0]


def build(plan: ChangeList, domain: str, when: str, include_flagged: Iterable[str] = ()) -> Dict[str, bytes]:
    """The files, by name: README.txt, review.csv, and each step's CSV that has rows. Raises ``HandoverError`` for an
    incomplete list, or when nothing is left to hand over."""
    if plan.incomplete:
        raise HandoverError("Part of Classroom couldn't be read, so this change list may be incomplete and isn't handed "
                            "over. Compare again.")
    changes, dependent = _split(plan, include_flagged)
    if not changes:
        raise HandoverError("There's nothing to hand over.")
    words = dict(KINDS)
    files: Dict[str, bytes] = {}
    review: List[Tuple[str, ...]] = [("change", "class", "alias", "person", "current", "new")]
    review += [tuple(csv_safe(v) for v in (words[c.kind], c.title, c.alias, c.person, c.before, c.after)) for c in changes]
    files["review.csv"] = _plain_csv(review)
    steps: List[Tuple[Step, int]] = []
    for step in STEPS:
        rows = [step.row(c) for c in changes if step.pick(c)]
        if rows:
            files[step.file] = _plain_csv([step.columns, *rows])
            steps.append((step, len(rows)))
    left_out = [words[k] for k in plan.flags if k not in set(include_flagged)]
    files["README.txt"] = _readme(domain, when, steps, left_out, dependent).encode("utf-8")
    return files


def _readme(domain: str, when: str, steps: List[Tuple[Step, int]], left_out: List[str], dependent: int) -> str:
    lines = [
        f"Classroom changes for {domain}, from GamGUI's compare at {when}.",
        "",
        "GamGUI did not make these changes and won't. You run them with GAM, outside GamGUI: they don't pass its preview,",
        "its confirmation or its audit log. GamGUI's Classroom support hasn't been checked on a real school domain, and",
        "neither have these commands. Try them on a test class first.",
        "",
        "Before you start:",
        "- Read review.csv: every change, one row each. It's safe to open in a spreadsheet.",
        "- Never open the numbered files in a spreadsheet and save them: their values must stay exactly as written.",
        "- Run the steps in order, from this folder, as an admin GAM is set up for. Each step finishes before the next.",
        "- Classes are named by GamGUI's alias (d:gg-...), so later steps find a class created in step 3.",
        "",
        "Steps:",
    ]
    for step, count in steps:
        lines += ["", f"{step.file[:2]}. {step.what} ({count:,} row{'s' if count != 1 else ''}):", f"    {gam_line(step)}"]
    if left_out:
        lines += ["", "Held back by the risk check and not included (tick them in GamGUI to include them): "
                  + ", ".join(left_out) + "."]
    if dependent:
        lines += ["", f"Also left out: {dependent:,} change{'s' if dependent != 1 else ''} that need{'' if dependent != 1 else 's'}"
                  " a held-back one (adding someone whose removal from that class is held back would fail)."]
    lines +=["", "Afterwards, open GamGUI's Plan an import and click Compare again: what's left is what didn't happen.", ""]
    return "\n".join(lines)
