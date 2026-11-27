"""The pull-request rules CI holds every PR to (scripts/pr_rules.py, pr-rules.yml): a typed title, and a fix's
failure-log entry. Each title here is one this repo has merged, or one the check exists to stop."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("pr_rules", ROOT / "scripts" / "pr_rules.py")
pr_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr_rules)

LOG = "docs/failure-log/2026-11-25-the-mock-gave-the-first-exit-code.md"


@pytest.mark.parametrize("title", [
    "feat: rename a user, delete a group, add an admin role, create and rename a shared drive (roadmap 5.1)",
    "chore: match the folder-move catalog text to what runs",
    "build(deps): Bump the codeql-action group with 3 updates",
    "chore: GAM 7.52.06 (automated bump)",
    "refactor!: the guard takes the previews it shows",
    'Revert "feat: something that broke"',
])
def test_a_typed_title_passes(title):
    assert pr_rules.problems(title, []) == []


@pytest.mark.parametrize("title", [
    "GAM 7.52.05 (automated bump)",       # the old automated title: no type
    "Fix the thing",
    "feature: a type this repo doesn't use",
    "feat:missing the space",
    "feat(): empty scope",
    "",
])
def test_an_untyped_title_is_refused(title):
    [problem] = pr_rules.problems(title, [])
    assert "doesn't start with a type" in problem


def test_a_fix_needs_a_failure_log_entry():
    assert "docs/failure-log/" in pr_rules.problems("fix: a store file that can't be read is set aside", [])[0]
    assert pr_rules.problems("fix(builder): the alias refusal says so", ["gamgui/x.py", LOG]) == []
    # The log's README, a file elsewhere, or a misnamed entry isn't an entry.
    for not_an_entry in ("docs/failure-log/README.md", "docs/RULE-FEEDBACK.md", "docs/failure-log/notes.md"):
        assert pr_rules.problems("fix: x", [not_an_entry]), not_an_entry


def test_another_type_needs_no_entry():
    assert pr_rules.problems("chore: tidy", []) == []
