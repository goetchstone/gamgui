#!/usr/bin/env python3
"""ci-ok's decision: did every blocking job of this run really run and pass?

Takes the path of the run's job list (GitHub's "jobs for a workflow run attempt" JSON) and reads the
workflow's `needs` context from the NEEDS variable, as JSON. Exits 0 only when every `needs` result is
"success" and every blocking job in the list concluded "success". Anything else fails, including a value
nobody expected: a job no runner picked up shows `cancelled` in the job list, yet reports something other
than failure, cancelled or skipped through `needs` (failure log 2026-11-04). So nothing here names the bad
values, and bad input raises, which fails too. Stdlib only: it runs on the runner's own python3.
"""

from __future__ import annotations

import json
import os
import sys

# Jobs allowed to end any way: this check itself, still running, and the jobs with continue-on-error.
# tests/test_workflow_safety.py holds NON_BLOCKING to ci.yml.
SELF = "ci-ok"
NON_BLOCKING = {"gam-compat (latest, non-blocking)"}


def failures(needs: dict, listing: dict) -> list[str]:
    """Why this run may not pass; empty only when every blocking job ran and succeeded."""
    jobs = [job for job in listing["jobs"] if job["name"] != SELF and job["name"] not in NON_BLOCKING]
    problems = []
    if not needs or not jobs:
        problems.append("nothing to check: no needs, or no blocking job in the list")
    if listing["total_count"] != len(listing["jobs"]):
        problems.append(f"the job list holds {len(listing['jobs'])} of {listing['total_count']} jobs")
    for need, value in needs.items():
        if value["result"] != "success":
            problems.append(f"needs.{need}.result is {value['result']!r}")
        # A job's name is its id, or its id and a detail: "test (macos-latest, py3.14)".
        if not any(job["name"] == need or job["name"].startswith(f"{need} (") for job in jobs):
            problems.append(f"no job in the list for {need}")
    problems += [f"{job['name']} concluded {job['conclusion']!r}" for job in jobs if job["conclusion"] != "success"]
    return problems


def main(argv: list[str]) -> int:
    needs = json.loads(os.environ["NEEDS"])
    with open(argv[1], encoding="utf-8") as f:
        listing = json.load(f)
    for need, value in needs.items():
        print(f"needs.{need}.result = {value['result']!r}")
    for job in listing["jobs"]:
        print(f"{job['name']}: {job['status']}/{job['conclusion']}")
    problems = failures(needs, listing)
    if problems:
        print("\nA blocking CI job did not pass:")
        print("\n".join(f"- {problem}" for problem in problems))
        return 1
    print("\nEvery blocking job ran and passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
