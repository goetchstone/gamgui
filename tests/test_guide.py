"""docs/guide.md (plan V6): its scope table is Setup's, and its links land."""

from __future__ import annotations

import re
from pathlib import Path

from gamgui.core.setup import DWD_SCOPES

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "guide.md"


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def test_the_guide_lists_exactly_the_delegation_scopes_setup_shows():
    listed = set(re.findall(r"`(https://[^`]+)`", GUIDE.read_text()))
    assert listed == {scope for scope, _ in DWD_SCOPES}


def test_every_relative_link_in_the_guide_resolves():
    text = GUIDE.read_text()
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://")):
            continue
        path, _, anchor = target.partition("#")
        doc = (GUIDE.parent / path).resolve() if path else GUIDE
        assert doc.is_file(), target
        if anchor:
            headings = re.findall(r"^#+ (.+)$", doc.read_text(), flags=re.M)
            assert anchor in {_slug(h) for h in headings}, target
