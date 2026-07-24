"""Templates must keep directory data (adversary: whatever Google/GAM hands back) inside attributes.

Group member emails and display names are attacker-influenced text. `| tojson` does NOT escape the
double quote (it targets <script> blocks and single-quoted attributes), so using it inside a
double-quoted attribute lets such a value close the attribute and inject new ones.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from gamgui.core.gam.models import GAMGroup, GAMUser, GroupMember
from gamgui.web.server import TEMPLATES

from .test_users_web import client  # noqa: F401  (app + connected-client fixtures)

TEMPLATE_DIR = Path(TEMPLATES.env.loader.searchpath[0])

# One value carrying every character that could end an attribute or a tag.
HOSTILE = "ev\"il' <b>on\"error=\"alert(1)\" &@example.com"


class _Attrs(HTMLParser):
    """Collects (tag, attrs-dict) for every start tag, as the browser's parser would see them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tags.append((tag, {k: (v or "") for k, v in attrs}))


def _parse(html: str) -> _Attrs:
    p = _Attrs()
    p.feed(html)
    return p


def _tag_with(parsed: _Attrs, attr: str) -> dict[str, str]:
    matches = [a for _, a in parsed.tags if attr in a]
    assert matches, f"no element carrying {attr} was rendered"
    assert len(matches) == 1
    return matches[0]


def _render(name: str, **ctx) -> str:
    return TEMPLATES.env.get_template(name).render(**ctx)


def test_board_member_email_cannot_break_out_of_its_attribute():
    html = _render("_board_members.html", group="staff@example.com", members=[GroupMember(email=HOSTILE)])

    card = _tag_with(_parse(html), "data-email")
    assert card["data-email"] == HOSTILE  # the whole value survives, inside the attribute
    assert "onerror" not in card          # ...and nothing leaked out as a new attribute
    assert "&#34;" in html                # the quote is escaped, not emitted raw
    assert 'on"error="alert(1)"' not in html


def test_board_group_name_cannot_break_out_of_hx_vals():
    # `group` comes from the select, but is echoed back into the partial's hx-vals payload.
    html = _render("_board_members.html", group=HOSTILE, members=[GroupMember(email="bob@example.com")])

    remove = _tag_with(_parse(html), "hx-vals")
    assert json.loads(remove["hx-vals"])["group"] == HOSTILE  # confined to the payload, intact
    assert "onerror" not in remove


def test_people_pool_user_cannot_break_out_of_its_attribute():
    html = _render(
        "groups.html",
        connected=True,
        users=[GAMUser(primary_email=HOSTILE, given_name=HOSTILE, family_name="")],
        groups=[GAMGroup(email=HOSTILE, name=HOSTILE)],
    )

    card = _tag_with(_parse(html), "data-email")
    assert card["data-email"] == HOSTILE
    assert "onerror" not in card
    assert 'on"error="alert(1)"' not in html


def test_drag_and_drop_still_reads_the_email_from_the_card():
    # Behaviour guard: the handler is wired to the element, and takes the email off data-email.
    pool = _render("groups.html", connected=True, users=[GAMUser(primary_email="a@example.com")], groups=[])
    board = _render("_board_members.html", group="staff@example.com", members=[GroupMember(email="a@example.com")])

    assert "gqDrag(event, this, 'add')" in pool
    assert "gqDrag(event, this, 'remove')" in board
    assert "el.dataset.email" in pool


def test_board_renders_over_http(client):  # noqa: F811
    r = client.get("/groups/members", params={"group": "team@example.com"})
    assert r.status_code == 200
    assert "gqDrag(event, this, 'remove')" in r.text


# --- the durable part: no template may put `| tojson` back into a double-quoted attribute ---

_TOJSON = re.compile(r"\|\s*tojson")


def _enclosing_quote(prefix: str) -> str:
    """The quote char of the attribute the expression sits in, or "" if it is not in one.

    Looks back to the nearest attribute opener; if its quote reappears before the expression the
    attribute already closed, so the expression is element/text content instead.
    """
    dq, sq = prefix.rfind('="'), prefix.rfind("='")
    if dq < 0 and sq < 0:
        return ""
    quote = '"' if dq > sq else "'"
    return "" if quote in prefix[max(dq, sq) + 2:] else quote


def _in_script_block(prefix: str) -> bool:
    return prefix.rfind("<script") > prefix.rfind("</script>")


def _tojson_uses() -> list[tuple[Path, int, str, bool]]:
    uses = []
    for path in sorted(TEMPLATE_DIR.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for m in _TOJSON.finditer(text):
            prefix = text[: m.start()]
            uses.append((path, prefix.count("\n") + 1, _enclosing_quote(prefix), _in_script_block(prefix)))
    return uses


def test_no_tojson_inside_a_double_quoted_attribute():
    bad = [f"{p.name}:{line}" for p, line, quote, in_script in _tojson_uses() if quote == '"' and not in_script]
    assert not bad, (
        "`| tojson` does not escape `\"`, so inside a double-quoted attribute a value containing one "
        f"breaks out of it — move the value to a plain data-* attribute instead: {bad}"
    )


@pytest.mark.parametrize("use", _tojson_uses(), ids=lambda u: f"{u[0].name}:{u[1]}")
def test_every_tojson_use_is_a_script_block_or_a_single_quoted_attribute(use):
    path, line, quote, in_script = use
    assert in_script or quote == "'", f"{path.name}:{line}: `| tojson` in an unsafe position"
