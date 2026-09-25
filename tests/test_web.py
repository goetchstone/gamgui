from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import TEST_HOSTS

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(tmp_path) -> TestClient:
    vault = SecretsVault(backend=InMemoryBackend())
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    state = AppState(vault=vault, runner=runner, audit_domain="", connector=None, token="testtoken")
    return TestClient(create_app(state, allowed_hosts=TEST_HOSTS))


def test_healthz_is_open(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_root_requires_token(client):
    assert client.get("/").status_code == 403


def test_root_with_token_renders_and_sets_cookie(client):
    r = client.get("/?token=testtoken")
    assert r.status_code == 200
    assert "GamGUI" in r.text  # neutral product name
    assert "Not connected" in r.text  # no creds in the in-memory vault
    # cookie now set on the client -> a token-less follow-up is allowed
    assert client.get("/").status_code == 200


def test_wrong_token_forbidden(client):
    assert client.get("/?token=nope").status_code == 403


def test_the_nav_marks_the_screen_you_are_on(client):
    # Plan U13: one link says aria-current="page" — a screen's detail or sub-page counts as the screen.
    client.get("/?token=testtoken")

    def current(path: str) -> list[str]:
        nav = client.get(path).text.split("</nav>", 1)[0]
        return re.findall(r'href="([^"]*)" aria-current="page"', nav[nav.index("<nav"):])
    assert current("/") == ["/"]
    assert current("/users") == ["/users"]
    assert current("/users/detail?email=a@example.com") == ["/users"]
    assert current("/audit") == ["/audit"]
    assert current("/setup") == []


def test_home_describes_every_screen_in_the_nav(client):
    # Home once said the shipped screens "come next"; tie its list to the nav so it can't go stale again.
    html = client.get("/?token=testtoken").text
    nav, body = html.split("</nav>", 1)
    screens = set(re.findall(r'href="(/[a-z]+)"', nav[nav.index("<nav"):]))
    assert screens >= {"/users", "/onboard", "/lifecycle", "/audit"}
    assert all(f'href="{s}"' in body for s in screens), screens - {s for s in screens if f'href="{s}"' in body}
    assert "come next" not in html
    assert 'href="/setup"' in body      # unconfigured: points at setup first


def test_the_header_is_laid_out_the_same_on_every_screen(client):
    # The header took each page's content width (container_class), so on Home and /setup (max-w-5xl) the
    # wordmark and nav sat inset while on the ten full-width screens they ran edge to edge — the nav moved
    # when you changed screens. The header's row is page-independent now.
    client.get("/?token=testtoken")                       # the launch token sets the session cookie
    rows = {}
    for path in ("/", "/setup", "/users", "/signatures"):
        html = client.get(path).text
        m = re.search(r"<header[^>]*>\s*<div class=\"([^\"]+)\"", html)
        assert m, f"{path}: no header row\n{html[html.find('<header'):html.find('<header') + 400]}"
        rows[path] = m.group(1)
    assert len(set(rows.values())) == 1, rows
    assert "container" not in rows["/"] and "max-w" not in rows["/"], rows
