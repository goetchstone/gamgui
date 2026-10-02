"""The failure log is one file per incident (docs/failure-log/README.md).

A single newest-first file made every two open PRs that logged an incident
conflict at the same line. These hold the layout that prevents it.
"""

import re
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "docs" / "failure-log"
NAME = re.compile(r"(\d{4}-\d{2}-\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*\.md")


def _entries():
    return sorted(p for p in LOG_DIR.glob("*.md") if p.name != "README.md")


def test_the_single_file_log_does_not_come_back():
    # A stale branch that prepends to the old file would recreate it on merge.
    assert not (LOG_DIR.parent / "failure-log.md").exists()
    assert len(_entries()) > 0


def test_each_entry_is_named_and_titled_by_its_date():
    for path in _entries():
        m = NAME.fullmatch(path.name)
        assert m, f"{path.name}: name it YYYY-MM-DD-<lowercase-slug>.md"
        first = path.read_text().splitlines()[0]
        assert first.startswith(f"# {m.group(1)} — "), f"{path.name}: open with '# {m.group(1)} — <title>'"


# Logged before this test with Cause and Why not caught never written; not reconstructed after the fact.
_INCOMPLETE = {"2026-09-24-bulk-onboarding-ran-whatever-came-back-and-a-cancelled-task.md"}


def test_each_entry_has_the_five_fields():
    fields = ("Symptom", "Cause", "Why not caught", "Fix", "Prevention")
    for path in _entries():
        if path.name in _INCOMPLETE:
            continue
        text = path.read_text()
        missing = [f for f in fields if f"**{f}" not in text]
        assert not missing, f"{path.name}: missing {missing}"


def test_every_relative_link_in_an_entry_resolves():
    # Entries moved one folder deeper when the log was split; a link written for docs/ breaks silently.
    broken = []
    for path in _entries():
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", path.read_text()):
            if not target.startswith(("http://", "https://", "mailto:")) and not (path.parent / target).exists():
                broken.append(f"{path.name} -> {target}")
    assert not broken, broken
