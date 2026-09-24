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
