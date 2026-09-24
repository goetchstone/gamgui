"""Templates must keep directory data (adversary: whatever Google/GAM hands back) inside attributes.

Group member emails and display names are attacker-influenced text. `| tojson` does NOT escape the
double quote (it targets <script> blocks and single-quoted attributes), so using it inside a
double-quoted attribute lets such a value close the attribute and inject new ones.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from gamgui.core.gam.models import GAMGroup, GAMUser, GroupMember
from gamgui.web.server import TEMPLATES

from .test_users_web import client  # noqa: F401  (app + connected-client fixtures)

TEMPLATE_DIR = Path(TEMPLATES.env.loader.searchpath[0])
STATIC_DIR = TEMPLATE_DIR.parent / "static"

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


def _field(parsed: _Attrs, name: str) -> dict[str, str]:
    """The one hidden input named ``name``."""
    matches = [a for tag, a in parsed.tags if tag == "input" and a.get("name") == name]
    assert len(matches) == 1, matches
    return matches[0]


def test_board_member_email_cannot_break_out_of_its_attribute():
    html = _render("_board_members.html", group="staff@example.com", members=[GroupMember(email=HOSTILE)],
                   total=1, all_count=1, page=1, pages=1, start=0)

    field = _field(_parse(html), "email")        # the row's Remove… form posts it back
    assert field["value"] == HOSTILE             # the whole value survives, inside the attribute
    assert "onerror" not in field                # ...and nothing leaked out as a new attribute
    assert "&#34;" in html                       # the quote is escaped, not emitted raw
    assert 'on"error="alert(1)"' not in html


def test_board_group_name_cannot_break_out_of_its_attribute():
    # `group` comes from the finder, and is echoed back into each row's form and the pager's URL.
    html = _render("_board_members.html", group=HOSTILE, members=[GroupMember(email="bob@example.com")] * 60,
                   total=60, all_count=60, page=1, pages=2, start=0)
    parsed = _parse(html)

    assert {a["value"] for tag, a in parsed.tags if a.get("name") == "group"} == {HOSTILE}
    pager = [a for _, a in parsed.tags if "hx-get" in a]
    assert pager and all("onerror" not in a and '"' not in a["hx-get"] for a in pager)
    confirm = _render("_group_remove_confirm.html", group=HOSTILE, email=HOSTILE, role="member", q="", page=1)
    assert _field(_parse(confirm), "group")["value"] == HOSTILE
    assert _field(_parse(confirm), "email")["value"] == HOSTILE


def test_finder_group_and_suggested_person_cannot_break_out_of_their_attributes():
    finder = _render("_group_results.html", groups=[GAMGroup(email=HOSTILE, name=HOSTILE)], selected="")
    button = _tag_with(_parse(finder), "data-group")
    assert button["data-group"] == HOSTILE and "onerror" not in button
    assert '"' not in button["hx-get"]

    people = _render("_group_people.html", people=[GAMUser(primary_email=HOSTILE, given_name=HOSTILE, family_name="")])
    option = _tag_with(_parse(people), "data-val")
    assert option["data-val"] == HOSTILE and "onerror" not in option


def test_groups_js_reads_addresses_off_data_attributes():
    # Behaviour guard: groups.js takes the picked group and the suggested person off their data-*
    # attributes (autoescaped by Jinja), never out of generated JS.
    js = (STATIC_DIR / "groups.js").read_text(encoding="utf-8")
    assert "btn.dataset.group" in js and "opt.dataset.val" in js
    page = _render("groups.html", connected=True, groups=[], selected="", group="")
    assert '<script src="/static/groups.js">' in page


def test_board_renders_over_http(client):  # noqa: F811
    r = client.get("/groups/members", params={"group": "team@example.com"})
    assert r.status_code == 200
    assert 'hx-post="/groups/members/remove/preview"' in r.text


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


# --- the CSP's script-src 'self' (plan Q13): no inline script, handler or eval may come back ---
# The policy (web/server.py) makes one inert anyway; these keep the templates from depending on one,
# which would silently break a button rather than fail a test.

_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
_HANDLER = re.compile(r"(?<![\w-])on[a-z]+\s*=", re.I)  # onclick=, oninput=, ... (not data-on…/hx-on)
_EVAL = re.compile(r"hx-on|hx-vars|javascript:|hx-(?:vals|headers)\s*=\s*[\"']\s*js:", re.I)
_TRIGGER = re.compile(r"hx-trigger\s*=\s*([\"'])(.*?)\1", re.S)


def _problems(name: str, html: str) -> list[str]:
    bad = []
    scripts = _SCRIPT.findall(html)
    if len(scripts) != html.lower().count("<script"):
        bad.append(f"{name}: a <script> that isn't a plain src + </script> pair")
    for attrs, body in scripts:
        src = re.search(r'\ssrc="/static/([^"?#]+)"', attrs)
        if not src or body.strip():
            bad.append(f"{name}: inline <script>{attrs}")
        elif not (STATIC_DIR / src.group(1)).is_file():
            bad.append(f"{name}: <script> names a missing /static/{src.group(1)}")
    bad += [f"{name}: inline handler {m.group(0)!r}" for m in _HANDLER.finditer(html)]
    bad += [f"{name}: eval-only htmx/JS {m.group(0)!r}" for m in _EVAL.finditer(html)]
    for _, spec in _TRIGGER.findall(html):  # an event filter, click[ctrlKey], is evaluated JS
        bad += [f"{name}: trigger filter {spec!r}" for part in spec.split(",") if "[" in (part.split() or [""])[0]]
    return bad


def test_no_template_carries_an_inline_script_handler_or_eval():
    bad = [b for p in sorted(TEMPLATE_DIR.rglob("*.html")) for b in _problems(p.name, p.read_text(encoding="utf-8"))]
    assert not bad, "move it into a same-origin gamgui/web/static/*.js file (data-action + a delegated listener): " + "; ".join(bad)


def test_only_full_pages_load_a_script():
    # htmx's allowScriptTags is off, so a <script src> in a swapped-in partial would silently not run:
    # a partial's behaviour belongs in a file its page already loads.
    partials = [p.name for p in TEMPLATE_DIR.rglob("*.html")
                if "<script" in (src := p.read_text(encoding="utf-8")) and p.name != "base.html" and "{% extends" not in src]
    assert not partials, partials


def test_the_scanner_sees_each_kind_of_inline_script():
    for html in ('<button onclick="x()">', "<input oninput='x'>", "<script>x()</script>", '<script src="https://cdn.example.com/x.js"></script>',
                 '<div hx-on:click="x()">', "<b hx-vals='js:{a: 1}'>", '<a href="javascript:x()">', '<div hx-trigger="click[ctrlKey]">'):
        assert _problems("t", html), html
    assert not _problems("t", '<script src="/static/app.js"></script><div data-action="copy" hx-trigger="keyup from:input[name=\'q\']">')


@pytest.mark.parametrize("path", ["/", "/users", "/users/detail?email=alice@example.com", "/groups", "/signatures",
                                  "/calendars", "/builder", "/onboard", "/lifecycle", "/reports", "/audit", "/setup"])
def test_each_screen_renders_without_inline_script(client, path):  # noqa: F811
    r = client.get(path)
    assert r.status_code == 200
    assert not _problems(path, r.text)


def test_every_data_action_has_a_handler():
    used = {a for p in TEMPLATE_DIR.rglob("*.html") for a in re.findall(r'data-action="([\w-]+)"', p.read_text(encoding="utf-8"))}
    shared = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    pages = "".join(p.read_text(encoding="utf-8") for p in STATIC_DIR.glob("*.js"))
    handled = set(re.findall(r'^\s*"([a-z][\w-]*)":', shared, re.M)) | set(re.findall(r'actions\["([\w-]+)"\]\s*=', pages))
    assert used and used <= handled, f"data-action with no handler in gamgui/web/static: {sorted(used - handled)}"
