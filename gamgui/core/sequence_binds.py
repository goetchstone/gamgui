"""Step-to-step dataflow in a Builder sequence (roadmap 5.2): a later step's slot takes its values from an earlier read.

The operator types `=step1.primaryEmail` in a slot: that step runs once for each value in the `primaryEmail` column of
step 1's result. Invariant 2 says a confirm runs what its preview held, and a value a write would produce doesn't exist
until it runs, so:

- **Only reads bind.** Step N must be an earlier step whose command is READ_ONLY.
- **One source a step.** Every bound slot in a step names the same step; its values are taken row by row.
- **Bound reads run at preview**, and the resolved steps, one per distinct row (first seen first), are what the preview
  shows, holds and runs. Each is built by its command's own builder, so each value is checked by the slot's validator.
- **A read that fell short is unknown, never none** (invariant 10): a failed read, output that isn't a table, or a row
  without the column (or with it empty) blocks the preview. A read with no rows resolves the step to none, said aloud.
- **Bounded:** at most MAX_RESOLVED_STEPS steps after resolving.

SwiftGamGui takes the same syntax, rules and wording.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

BIND = re.compile(r"^=step(\d{1,3})\.(\S(?:.*\S)?)$")
MAX_RESOLVED_STEPS = 500


def parse_bind(value: str) -> Optional[Tuple[int, str]]:
    """``(step number, column)`` for a `=stepN.Column` value, else None: anything else is the value itself."""
    m = BIND.match((value or "").strip())
    return (int(m.group(1)), m.group(2)) if m else None


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return ""   # a list or record isn't one value: unknown, not a value
    return str(value).strip()


def values_from(records: Optional[List[dict]], columns: Sequence[str], source: int, step: int) -> Tuple[List[Tuple[str, ...]], str]:
    """The distinct value tuples (one per row, first seen first) of ``columns`` in a read's ``records``, or a refusal in
    fixed words when the read can't say who the step runs for. ``records`` None is output that wasn't a table."""
    if records is None:
        return [], f"Step {source}'s read didn't print a table, so who step {step} runs for isn't known. Nothing will run."
    seen: Dict[Tuple[str, ...], None] = {}
    for row in records:
        values = tuple(_cell(row.get(c)) for c in columns)
        missing = [c for c, v in zip(columns, values, strict=True) if not v]
        if missing:
            return [], (f"Step {source}'s result has a row with no {missing[0]}, so who step {step} runs for isn't "
                        f"known. Nothing will run. (Its columns: {', '.join(sorted({k for r in records for k in r})[:12])}.)")
        seen.setdefault(values, None)
    return list(seen), ""
