"""No GitHub Actions `${{ … }}` expression may be expanded inside a `run:` script.

The runner substitutes expressions into the script text *before* the shell parses it, so a value
like an upstream release tag (`gam-watch.yml`) becomes code: a tag such as `7.49.0$(curl …|sh)` ran
in a job holding `contents: write`. Values reach a script through `env:` instead, where the shell
sees them as data. A text scan (no YAML dependency) of every workflow's `run:` blocks.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.y*ml"))
_RUN = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*)$")


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
