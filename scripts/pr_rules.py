"""The rules a pull request is held to that no test can see: its title, and a fix's failure-log entry (pr-rules.yml).

A squash merge makes the title the commit on main, so the title carries the change's type: `feat:`, `fix:`, `chore:`
and the rest, an optional `(scope)`, then words. A `fix:` repairs something that broke, so it adds an entry to
docs/failure-log/ (`YYYY-MM-DD-<slug>.md`: symptom, cause, why not caught, fix, prevention; CONTRIBUTING.md, the
post-failure skill). A change that isn't a regression's repair takes another type. The maintainer's local hook held
commits to the same rule; this holds every pull request to it.

    PR_TITLE=… BASE_SHA=… HEAD_SHA=… python3 -I scripts/pr_rules.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Iterable, List

TYPES = ("feat", "fix", "chore", "docs", "test", "refactor", "perf", "build", "ci", "style", "revert")
TITLE = re.compile(r"^(?P<type>[a-z]+)(?:\([^()\s][^()]*\))?!?: \S")
FAILURE_LOG = re.compile(r"^docs/failure-log/\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.md$")


def problems(title: str, added: Iterable[str]) -> List[str]:
    """What keeps a pull request with this title, adding these files, from merging: [] when nothing does."""
    if title.startswith('Revert "'):   # GitHub's own Revert button
        return []
    m = TITLE.match(title)
    if not m or m["type"] not in TYPES:
        return [f"The title {title!r} doesn't start with a type: one of {', '.join(TYPES)}, an optional (scope), "
                "then a colon and the change in words, as in `feat: rename a user (roadmap 5.1)`."]
    if m["type"] == "fix" and not any(FAILURE_LOG.match(path) for path in added):
        return ["A fix: adds an entry to docs/failure-log/ (YYYY-MM-DD-<slug>.md: symptom, cause, why not caught, fix, "
                "prevention). If this isn't a repair of something that broke, give it another type."]
    return []


def added_files(base: str, head: str) -> List[str]:
    out = subprocess.run(["git", "diff", "--name-only", "--diff-filter=A", f"{base}...{head}"],
                         check=True, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]


def main() -> int:
    found = problems(os.environ["PR_TITLE"], added_files(os.environ["BASE_SHA"], os.environ["HEAD_SHA"]))
    for problem in found:
        print(f"::error::{problem}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
