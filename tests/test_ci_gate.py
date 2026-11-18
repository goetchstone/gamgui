"""scripts/ci_gate.py, the decision behind the required ci-ok check, on the job lists of real runs.

`tests/fixtures/ci_runs.json` is GitHub's own answer for each run, kept to the fields the gate reads. What
a job no runner picked up reports through `needs` has never been seen, so those runs are checked with every
`needs` result claiming success: the job list alone has to fail them (failure log 2026-11-04).
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNS = json.loads((ROOT / "tests" / "fixtures" / "ci_runs.json").read_text())
NEED_IDS = ["lint", "test", "a11y", "gam-compat", "app", "fuzz"]


def _gate():
    spec = importlib.util.spec_from_file_location("ci_gate", ROOT / "scripts" / "ci_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GATE = _gate()


def _needs(**results) -> dict:
    return {need: {"result": results.get(need.replace("-", "_"), "success"), "outputs": {}} for need in NEED_IDS}


def _live(run: str) -> dict:
    """The run as the gate sees it: its own job is still going, with no conclusion yet."""
    listing = copy.deepcopy(RUNS[run])
    for job in listing["jobs"]:
        if job["name"] == "ci-ok":
            job.update(status="in_progress", conclusion=None)
    return listing


@pytest.mark.parametrize("run", ["starved_on_pr", "starved_on_main", "cancelled_by_hand", "failed"])
def test_a_run_with_a_job_that_did_not_pass_fails_even_if_needs_claims_success(run):
    listing = _live(run)
    bad = {job["name"] for job in listing["jobs"]
           if job["name"] != "ci-ok" and job["conclusion"] != "success"}
    problems = GATE.failures(_needs(), listing)
    assert bad and all(any(problem.startswith(name) for problem in problems) for name in bad)


@pytest.mark.parametrize("run", ["all_passed", "rerun_passed"])
def test_a_run_where_every_job_passed_passes(run):
    assert GATE.failures(_needs(), _live(run)) == []


# Whatever `needs` reports for a job, only "success" passes: nothing here lists the bad values.
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "abandoned", "", None])
def test_any_needs_result_but_success_fails_even_if_the_job_list_is_green(result):
    assert GATE.failures(_needs(app=result), _live("all_passed")) == [f"needs.app.result is {result!r}"]


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", "timed_out", "neutral", "action_required", "", None])
def test_any_job_conclusion_but_success_fails_even_if_needs_claims_success(conclusion):
    listing = _live("all_passed")
    for job in listing["jobs"]:
        if job["name"] == "app (build + smoke check)":
            job["conclusion"] = conclusion
    assert GATE.failures(_needs(), listing) == [f"app (build + smoke check) concluded {conclusion!r}"]


def test_a_failed_non_blocking_job_does_not_fail_the_run():
    listing = _live("all_passed")
    for job in listing["jobs"]:
        if job["name"] in GATE.NON_BLOCKING:
            job["conclusion"] = "failure"
    assert GATE.failures(_needs(), listing) == []


def test_a_need_with_no_job_in_the_list_fails():
    listing = _live("all_passed")
    listing["jobs"] = [job for job in listing["jobs"] if not job["name"].startswith("app (")]
    listing["total_count"] = len(listing["jobs"])
    assert GATE.failures(_needs(), listing) == ["no job in the list for app"]


def test_the_non_blocking_preview_does_not_stand_in_for_gam_compat():
    # "gam-compat (latest, non-blocking)" starts with the blocking job's id too.
    listing = _live("all_passed")
    listing["jobs"] = [job for job in listing["jobs"] if job["name"] != "gam-compat (pinned binary)"]
    listing["total_count"] = len(listing["jobs"])
    assert GATE.failures(_needs(), listing) == ["no job in the list for gam-compat"]


def test_a_truncated_job_list_fails():
    listing = _live("all_passed")
    listing["total_count"] += 1
    assert GATE.failures(_needs(), listing) == ["the job list holds 13 of 14 jobs"]


@pytest.mark.parametrize(("needs", "jobs"), [({}, None), (None, [])])
def test_nothing_to_check_fails(needs, jobs):
    listing = _live("all_passed")
    if jobs is not None:
        listing.update(jobs=jobs, total_count=0)
    assert GATE.failures(_needs() if needs is None else needs, listing)


def test_the_script_exits_by_its_decision_and_prints_what_needs_said(tmp_path, monkeypatch, capsys):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(_live("all_passed")))
    monkeypatch.setenv("NEEDS", json.dumps(_needs()))
    assert GATE.main(["ci_gate.py", str(path)]) == 0
    assert "needs.a11y.result = 'success'" in capsys.readouterr().out
    path.write_text(json.dumps(_live("starved_on_main")))
    assert GATE.main(["ci_gate.py", str(path)]) == 1
    assert "a11y (axe-core ratchet) concluded 'cancelled'" in capsys.readouterr().out


@pytest.mark.parametrize("env", ["not json", "{}"])
def test_bad_input_never_exits_zero(tmp_path, monkeypatch, env):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(_live("all_passed")))
    monkeypatch.setenv("NEEDS", env)
    try:
        assert GATE.main(["ci_gate.py", str(path)]) != 0
    except ValueError:
        pass    # an exception is a non-zero exit


def test_a_needs_entry_without_a_result_never_exits_zero(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(_live("all_passed")))
    monkeypatch.setenv("NEEDS", json.dumps({"app": {}}))
    with pytest.raises(KeyError):
        GATE.main(["ci_gate.py", str(path)])


# --- a rerun of the failed jobs (failure log 2026-11-18) ---------------------------------------------------------
# ci-ok reads every attempt's jobs (`filter=all`) and judges each job by its newest attempt: a rerun of the failed
# jobs copies the rest into its own attempt only later, so the attempt's own list lacked jobs that had passed.

def _attempt(listing: dict, attempt: int) -> dict:
    jobs = [job for job in listing["jobs"] if job.get("run_attempt", 1) == attempt]
    return {**listing, "jobs": jobs, "total_count": len(jobs)}


def test_a_rerun_of_the_failed_jobs_passes_on_what_every_attempt_ran():
    assert GATE.failures(_needs(), _live("partial_rerun_listed_late")) == []


def test_the_rerun_attempt_alone_is_what_failed_it():
    problems = GATE.failures(_needs(), _attempt(_live("partial_rerun_listed_late"), 4))
    assert "no job in the list for gam-compat" in problems and "no job in the list for app" in problems


def test_a_job_judged_by_its_newest_attempt_fails_when_that_attempt_dropped_it():
    listing = _live("partial_rerun_listed_late")
    for job in listing["jobs"]:
        if job["name"] == "test (macos-latest, py3.14)" and job["run_attempt"] == 4:
            job["conclusion"] = "cancelled"       # passed in attempt 3, rerun and dropped in attempt 4
    assert GATE.failures(_needs(), listing) == ["test (macos-latest, py3.14) concluded 'cancelled'"]


def test_an_earlier_attempts_failure_is_overruled_by_a_newer_pass():
    listing = _live("partial_rerun_listed_late")
    older = [job for job in listing["jobs"] if job["name"] == "app (build + smoke check)"]
    assert any(job["conclusion"] == "cancelled" for job in older) and GATE.failures(_needs(), listing) == []
