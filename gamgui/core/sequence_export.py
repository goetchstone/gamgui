"""A Builder sequence written out as a `gam batch` file (roadmap 5.3), so it can run under GAM's own batch machinery.

GAM 7.52.05's doBatch reads the file a line at a time (Python's text iteration: `\\n`, `\\r` and `\\r\\n` end a line),
skips a line that starts with `#`, and splits each with `shlex.split` (POSIX, no comments), so every argv element is
`shlex.quote`d and a value holding a line break or a NUL is refused: it would end the line mid-argument. `%word%` is
substituted only after a `set` or `datetime` line, and none is written. `gam batch` runs its lines in parallel
processes, so a `commit-batch` line between steps makes each wait for the one before, as the sequence does.

The file then runs outside GamGUI's guard, preview and audit; the screen says so and the export itself is audited.
A step that sets a password is refused, not written: the file would keep it in plain text.

SwiftGamGui writes the same bytes for the same steps (tests/fixtures/sequence_export.txt holds both to it).
"""

from __future__ import annotations

import shlex
from typing import List, Sequence

# GAM's own password words (`update user … password random`): nothing secret is written for these.
_GENERATED = {"random", "uniquerandom", "blocklogin"}
_BREAKS = ("\n", "\r", "\0")

HEADER = (
    "# A GamGUI Builder sequence, written for `gam batch <this file>`.",
    "# It runs outside GamGUI: no preview, no confirmation, no audit record. Read every line before you run it.",
    "# Each commit-batch waits for the step before it, so the steps run in this order.",
)


class ExportRefused(ValueError):
    """A step that can't be written safely; the message says which and why, in the operator's words."""


def batch_line(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(a) for a in ["gam", *argv])


def refusal(n: int, label: str, argv: Sequence[str]) -> str:
    """Why step n can't be written, or "" when it can."""
    words = [a.lower() for a in argv]
    for i, w in enumerate(words):
        if w == "password" and (i + 1 >= len(words) or words[i + 1] not in _GENERATED):
            return f"Step {n} ({label}) sets a password, which the file would keep in plain text. Remove it to export."
    if any(b in a for a in argv for b in _BREAKS):
        return f"Step {n} ({label}) has a value with a line break in it, which a gam batch file can't hold."
    if not argv:
        return f"Step {n} ({label}) has no command."
    return ""


def why_not(steps: Sequence[dict]) -> str:
    """Why these steps can't be written as a batch file, in fixed words, or "" when they can."""
    if not steps:
        return "The sequence is empty."
    for n, step in enumerate(steps, 1):
        why = refusal(n, str(step.get("label", "")), [str(a) for a in step["argv"]])
        if why:
            return why
    return ""


def batch_file(steps: Sequence[dict]) -> str:
    """The batch file for a sequence's steps (each a dict with `argv` and `label`); ExportRefused when ``why_not`` says."""
    why = why_not(steps)
    if why:
        raise ExportRefused(why)
    lines: List[str] = list(HEADER)
    for n, step in enumerate(steps, 1):
        argv = [str(a) for a in step["argv"]]
        if n > 1:
            lines.append("commit-batch")
        lines.append(f"# {n}. {step.get('label', '')}".replace("\n", " ").replace("\r", " "))
        lines.append(batch_line(argv))
    return "\n".join(lines) + "\n"
