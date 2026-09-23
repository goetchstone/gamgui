"""scripts/preview_mock.py is how a session looks at a screen; keep it booting against today's app."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).parent.parent


def _preview():
    spec = importlib.util.spec_from_file_location("preview_mock", ROOT / "scripts" / "preview_mock.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_preview_serves_the_seeded_screens_on_its_loopback_port(tmp_path, monkeypatch):
    mod = _preview()
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(mod.FIXTURES))
    app = mod.build_app(tmp_path)
    with TestClient(app, base_url=f"http://127.0.0.1:{mod.PORT}") as c:
        assert c.get(f"/?token={mod.TOKEN}").status_code == 200
        assert "Sales Associate" in c.get("/onboard").text
        assert "Company Holidays" in c.get("/calendars/search", params={"q": "holidays"}).text
        assert "alice@example.com" in c.get("/users").text
    assert (tmp_path / "onboarding.json").exists()      # state went to the dir it was given
