"""Text contrast (plan A1): every text colour the UI can paint clears WCAG AA (4.5:1).

Read off the committed app.css — what the browser actually gets — so a template that adds a failing
colour fails here once `make css` has run (CI's lint job fails a stale app.css first). The pages are
paper and white cards; axe (tests/test_a11y.py) checks the real backgrounds of the states it visits,
this checks every class, visited or not. Before A1: brand-gray text was 2.07:1 on white.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "gamgui" / "web" / "static" / "app.css").read_text()
AA = 4.5
PAPER, WHITE = (250, 249, 246), (255, 255, 255)

# `.text-x`, `.hover\:text-x:hover`, `.placeholder\:text-x::placeholder`, `.text-x\/90` → colour, alpha.
_RULE = r"^\.((?:[\w-]+\\:)*{kind}-[\w\\/.\[\]#-]+)(?:::?[\w-]+)* \{{\n(?:  --tw-{kind}-opacity: 1;\n)?" \
        r"  {prop}: rgb\((\d+) (\d+) (\d+) / (?:var\(--tw-{kind}-opacity, 1\)|([\d.]+))\);"


def _rules(kind: str, prop: str) -> dict[str, tuple[tuple[int, int, int], float]]:
    rx = re.compile(_RULE.format(kind=kind, prop=prop), re.M)
    return {m[1].replace("\\", ""): ((int(m[2]), int(m[3]), int(m[4])), float(m[5] or 1))
            for m in rx.finditer(CSS)}


def _lum(rgb) -> float:
    c = [v / 255 for v in rgb]
    c = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(fg, bg, alpha: float = 1.0) -> float:
    fg = tuple(round(alpha * f + (1 - alpha) * b) for f, b in zip(fg, bg, strict=True))     # composite, as painted
    hi, lo = sorted((_lum(fg), _lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT = _rules("text", "color")
FILL = {name.split(":")[-1]: v for name, v in _rules("bg", "background-color").items()}   # hover:bg-x too
LIGHT = {name for name in TEXT if re.search(r"text-(brand-)?white\b", name)}   # labels on dark fills


def test_the_css_parser_sees_the_text_colours():
    assert {"text-ink", "text-brand-blueink", "text-brand-grayink", "text-brand-white"} <= TEXT.keys()
    assert {"bg-brand-blue", "bg-amber-800"} <= FILL.keys()


def test_every_dark_text_colour_clears_aa_on_paper_and_white():
    low = {name: f"{min(ratio(rgb, PAPER, a), ratio(rgb, WHITE, a)):.2f}:1"
           for name, (rgb, a) in TEXT.items() if name not in LIGHT and not name.startswith("disabled:")
           and min(ratio(rgb, PAPER, a), ratio(rgb, WHITE, a)) < AA}
    assert not low, f"text colours under {AA}:1 on paper or white (brand-gray is for borders only): {low}"


def test_every_fill_under_white_text_clears_aa():
    """A class list that paints light text (a button's label) — its opaque fills must carry it."""
    sources = [*(ROOT / "gamgui" / "web" / "templates").rglob("*.html"), *(ROOT / "gamgui" / "web" / "static").glob("*.js")]
    low = {}
    for path in sources:
        for classes in re.findall(r'"([^"\n]*)"', path.read_text()):
            tokens = {t.split(":")[-1] for t in re.findall(r"[\w:/.-]+", classes)}
            if not tokens & {name.split(":")[-1] for name in LIGHT}:
                continue
            for fill in tokens & FILL.keys():
                rgb, a = FILL[fill]
                if a == 1 and ratio(WHITE, rgb) < AA:
                    low[fill] = f"{ratio(WHITE, rgb):.2f}:1 in {path.name}"
    assert not low, f"white text on a fill under {AA}:1: {low}"


BORDER = {name: v for name, v in _rules("border", "border-color").items() if ":" not in name}   # resting state
NON_TEXT = 3.0                                                                                # WCAG 1.4.11
_FIELD = re.compile(r"<(?:input|select|textarea)\b[^>]*>", re.S)
_NOT_A_FIELD = re.compile(r'type="(?:hidden|checkbox|radio|submit|button|file)"')


def field_borders() -> dict[str, list[str]]:
    """Each template's text fields, selects and textareas → the border colours their classes paint
    (a `{{ var }}` class resolved from the file's own `{% set var = "…" %}`)."""
    out: dict[str, list[str]] = {}
    for path in sorted((ROOT / "gamgui" / "web" / "templates").rglob("*.html")):
        text = path.read_text()
        sets = dict(re.findall(r'\{%\s*set (\w+) = "([^"]*)" %\}', text))
        for m in _FIELD.finditer(text):
            if _NOT_A_FIELD.search(m[0]):
                continue
            classes = " ".join(re.findall(r'class="([^"]*)"', m[0]))
            tokens = re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda v, sets=sets: sets.get(v[1], ""), classes).split()
            if any(re.fullmatch(r"border(-\d)?", t) for t in tokens):
                line = text.count("\n", 0, m.start()) + 1
                out[f"{path.name}:{line}"] = [t for t in tokens if t in BORDER]
    return out


def test_every_form_control_border_clears_3_to_1():
    """Plan A-left2: a field's border is what shows there is a field (WCAG 1.4.11, 3:1 against what's
    next to it). brand-gray at 40–60% was ~1.5:1; `border-brand-field` is the gray darkened to clear it.
    Decorative card and rule borders aren't controls and stay light."""
    fields = field_borders()
    assert len(fields) > 40                             # the parser still finds the app's fields
    low = {}
    for where, colours in fields.items():
        if not colours:
            low[where] = "no border colour (Tailwind's default gray-200, 1.2:1)"
        for c in colours:
            rgb, a = BORDER[c]
            worst = min(ratio(rgb, PAPER, a), ratio(rgb, WHITE, a))
            if worst < NON_TEXT:
                low[where] = f"{c} {worst:.2f}:1"
    assert not low, f"form-control borders under {NON_TEXT}:1 on paper or white: {low}"
