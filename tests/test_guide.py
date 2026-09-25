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


# --- The guide's bold labels are the words on the screen it's describing -------------------
# The offboarding section once said to click **Preview** on a screen whose button reads "Preview
# steps", and the onboarding one said **Run** for a button reading "Create account & run
# onboarding". A label is checked against its own section's screen, so a word that exists
# elsewhere in the app ("Preview" is Onboard's button) doesn't pass for the wrong screen.

TEMPLATES = ROOT / "gamgui" / "web" / "templates"

# Each section of the guide → the templates of the screen it walks through. base.html (the nav and
# the header) counts for every section.
SCREENS = {
    "First setup": ["setup.html", "_dwd.html", "_verify.html", "_tenant.html"],
    "Find a user": ["users.html", "_users_table.html", "_as_of.html"],
    "Onboard a new hire": ["onboarding.html", "_onboard_*.html"],
    "Offboard a leaver": ["lifecycle.html", "_offboard_*.html", "_job_*.html"],
    "Roll out a signature": ["signatures.html", "_sig_*.html", "_job_*.html"],
    "Share a calendar": ["calendars.html", "_calendar*.html", "user_detail.html"],
    "Audit external sharing": ["builder.html", "_builder_*.html", "_read_output.html", "_records_*.html"],
    "Jobs and Stop": ["job.html", "_job*.html"],
    "Read the audit log": ["audit.html", "_audit_rows.html"],
}

# Bold that isn't a control: emphasis, run-in headings, and Google's or macOS's own buttons.
NOT_A_LABEL = {
    "confirmed", "not yet", "One person", "A CSV of hires", "quitting the app ends them",
    "Always Allow",   # macOS's Keychain dialog
    "Authorize",      # Google Admin Console's button
}


def _screen_words(patterns: list[str]) -> set[str]:
    """Every piece of text a screen shows: element text split at Jinja tags, plus quoted literals
    (the nav's labels live in a Jinja tuple). ``{{ n }}`` reads as N, and a trailing arrow is
    dropped, the way the guide writes them."""
    words: set[str] = set()
    for pattern in ["base.html", *patterns]:
        for path in TEMPLATES.glob(pattern):
            html = path.read_text()
            pieces = re.findall(r">([^<>]+)<", html) + re.findall(r'"([^"<>{}]+)"', html)
            for piece in pieces:
                for part in re.split(r"\{%.*?%\}", piece.replace("&amp;", "&")):
                    # "Retry the {{ n }} that failed" reads as N; "Jobs{{ count }}" is the word Jobs.
                    for text in (re.sub(r"\{\{.*?\}\}", "N", part), *re.split(r"\{\{.*?\}\}", part)):
                        text = " ".join(text.split()).rstrip(" ↓↗…")
                        if text:
                            words.add(text)
    return words


def _catalog_words() -> set[str]:
    import json

    catalog = json.loads((ROOT / "gamgui" / "resources" / "gam7" / "command_catalog.json").read_text())
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            found.add(node)

    walk(catalog)
    return found


def _sections() -> dict[str, str]:
    parts = re.split(r"^## (.+)$", GUIDE.read_text(), flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_every_guide_section_is_mapped_to_its_screen():
    assert set(_sections()) == set(SCREENS)


def test_every_bold_label_in_the_guide_is_on_its_screen():
    missing = []
    for section, text in _sections().items():
        words = _screen_words(SCREENS[section])
        if section == "Audit external sharing":
            words |= _catalog_words()   # "Users → Drive → Print drivefileacls" is the catalog's path
        for span in re.findall(r"\*\*(.+?)\*\*", text, flags=re.S):
            span = " ".join(span.split())
            if span.endswith((":", ".")) or "](" in span or span in NOT_A_LABEL:
                continue   # a run-in heading ("Before:"), a sentence, a link
            for label in (part.strip() for part in span.split("→")):
                if label not in words:
                    missing.append(f"{section}: **{label}**")
    assert not missing, "the guide names a control its screen doesn't show:\n" + "\n".join(missing)
