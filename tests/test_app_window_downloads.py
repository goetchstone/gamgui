"""Every file download must download in the app's own window, not open as a page in it.

The app shows pages in pywebview's WKWebView. Its macOS backend downloads a response only when WebKit
can't display it, or when the link carries the HTML `download` attribute
(`webView:decidePolicyForNavigationAction:` → `shouldPerformDownload`). A CSV is text, so WebKit
displays it: without the attribute the window navigated to the CSV, ignoring
`Content-Disposition: attachment`, and the window has no Back (failure-log 2026-10-08). A browser
downloads either way, which is why only the app showed it. These hold every attachment route to a
`download` link.
"""

from __future__ import annotations

import re
from pathlib import Path

from gamgui.web.server import TEMPLATES

TEMPLATE_DIR = Path(TEMPLATES.env.loader.searchpath[0])
ROUTES_DIR = TEMPLATE_DIR.parent / "routes"
# How a template names each download route: a literal path, or the variable that carries it.
HREFS = {"/builder/export.csv": "{{ csv_url }}"}


def _attachment_paths() -> set:
    """GET paths whose handler sends Content-Disposition: attachment (a file to save, not a page)."""
    paths = set()
    for path in ROUTES_DIR.glob("*.py"):
        src = path.read_text()
        prefix = (re.search(r'APIRouter\(prefix="([^"]*)"', src) or [None, ""])[1]
        for m in re.finditer(r'@router\.get\("([^"]*)"[^)]*\)\s*\n(?:async )?def \w+\([^)]*\)[^:]*:(.*?)(?=\n@router|\Z)',
                             src, re.S):
            if "attachment" in m.group(2):
                paths.add(prefix + m.group(1))
    return paths


def _anchors() -> list:
    return [(p.name, m.group(0)) for p in TEMPLATE_DIR.glob("*.html") for m in re.finditer(r"<a\s[^>]*>", p.read_text())]


def test_the_attachment_routes_are_the_ones_we_think():
    # A new download route must be added here and linked with `download`; this catches it appearing.
    assert _attachment_paths() == {"/audit/export.csv", "/builder/export.csv", "/onboard/bulk/template.csv"}


def test_every_link_to_a_download_route_carries_the_download_attribute():
    missing, linked = [], set()
    for route in _attachment_paths():
        href = HREFS.get(route, route)
        for name, tag in _anchors():
            if f'href="{href}' in tag:
                linked.add(route)
                if not re.search(r"\sdownload[\s>=]", tag):
                    missing.append(f"{name}: {tag[:90]}")
    assert not missing, "a CSV link opens as a page in the app window without `download`:\n" + "\n".join(missing)
    assert linked == _attachment_paths()                                 # every route is linked somewhere
