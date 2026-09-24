"""Rules for the workflows, checked by a text scan (no YAML dependency).

No GitHub Actions `${{ … }}` expression may be expanded inside a `run:` script. The runner
substitutes expressions into the script text *before* the shell parses it, so a value like an
upstream release tag (`gam-watch.yml`) becomes code: a tag such as `7.49.0$(curl …|sh)` ran in a job
holding `contents: write`. Values reach a script through `env:` instead, where the shell sees them as
data.

Every action is pinned by full commit SHA. A tag is mutable: whoever controls the action's repo can
move `v7` to new code, which then runs in jobs holding a token (`gam-watch.yml` can push and open
PRs). The trailing `# vX.Y.Z` comment is what Dependabot's `github-actions` ecosystem reads to bump
the SHA and the comment together.

CI keeps a coverage floor: one test leg runs `pytest --cov` against `fail_under` in pyproject.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
_RUN = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*)$")
_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(.*?)\s*$")
_PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40} # v\d+(?:\.\d+)*$")


def _run_scripts(text: str):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _RUN.match(line)
        if not m:
            continue
        indent, rest = len(m.group(1)), m.group(2)
        if rest and rest[0] not in "|>":
            yield i + 1, rest
            continue
        for j in range(i + 1, len(lines)):
            body = lines[j]
            if body.strip() and len(body) - len(body.lstrip()) <= indent:
                break
            yield j + 1, body


def test_workflows_exist():
    assert WORKFLOWS, "no workflow files found — the scan below would pass vacuously"


def test_no_expression_inside_run_scripts():
    bad = [
        f"{wf.name}:{lineno}: {src.strip()}"
        for wf in WORKFLOWS
        for lineno, src in _run_scripts(wf.read_text())
        if "${{" in src
    ]
    assert not bad, "pass these through env: instead:\n" + "\n".join(bad)


def test_scanner_catches_the_gam_watch_shape():
    sample = (
        "      - name: Bump\n"
        "        run: python scripts/bump_gam.py \"v${{ steps.check.outputs.latest }}\"\n"
        "      - name: Block\n"
        "        run: |\n"
        "          echo ok\n"
        "          echo \"${{ github.event.issue.title }}\"\n"
        "      - name: Safe\n"
        "        env:\n"
        "          T: ${{ github.token }}\n"
    )
    hits = [src for _, src in _run_scripts(sample) if "${{" in src]
    assert len(hits) == 2


def _unpinned(text: str) -> list:
    return [(i, m.group(1)) for i, line in enumerate(text.splitlines(), 1)
            if (m := _USES.match(line)) and not m.group(1).startswith("./") and not _PINNED.match(m.group(1))]


def test_every_action_is_pinned_by_commit_sha():
    uses = [line for wf in WORKFLOWS for line in wf.read_text().splitlines() if _USES.match(line)]
    assert uses, "no uses: lines found — the scan below would pass vacuously"
    bad = [f"{wf.name}:{i}: {src}" for wf in WORKFLOWS for i, src in _unpinned(wf.read_text())]
    assert not bad, "pin by full commit SHA with the version in a comment (`@<sha> # vX.Y.Z`):\n" + "\n".join(bad)


def test_pin_scanner_rejects_tags_and_short_shas():
    sample = (
        "      - uses: actions/checkout@v7\n"
        "      - uses: actions/checkout@3d3c42e\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"         # no version comment
        "        uses: github/codeql-action/init@main # v4\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
        "        uses: github/codeql-action/init@2892aa5e19bbd11bc0cff5427e3b750a04d9e3c2 # v4.38.2\n"
        "      - uses: ./.github/actions/local\n"
    )
    assert [i for i, _ in _unpinned(sample)] == [1, 2, 3, 4]


def test_dependabot_keeps_the_pinned_actions_current():
    # A SHA pin never moves by itself; without this ecosystem the pins would only ever go stale.
    assert re.search(r'package-ecosystem:\s*"github-actions"', (ROOT / ".github" / "dependabot.yml").read_text())


def test_ci_enforces_the_coverage_floor():
    # pytest-cov applies fail_under only to a --cov run, so the gate is both halves together.
    report = re.search(r"^\[tool\.coverage\.report\]\n(?:.*\n)*?fail_under = (\d+)",
                       (ROOT / "pyproject.toml").read_text(), re.M)
    assert report and int(report[1]) >= 90, "the coverage floor in pyproject was removed or lowered"
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert re.search(r"^\s+run: pytest -q --cov\b", ci, re.M), "no CI step runs the suite under --cov"
