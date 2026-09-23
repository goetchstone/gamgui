"""The entry point: window-sizing math (use the display but always fit it, incl. 13\" Macs), and
the no-pywebview browser fallback."""

import sys

import pytest

from gamgui import app as app_module
from gamgui.app import _fit_size, _serve_in_browser

# (screen_w, screen_h) for displays GamGUI runs on.
SCREENS = [
    (1440, 900),    # 13" MacBook Air (default scaled)
    (1280, 800),    # 13" "more space" off / older default
    (1512, 982),    # 14" MacBook Pro (default scaled)
    (1366, 768),    # small external / older laptop
    (1680, 1050),   # 15"/16" scaled
    (3840, 2160),   # large 4K external
    (2560, 1440),   # 27" external
]


@pytest.mark.parametrize("sw,sh", SCREENS)
def test_window_always_fits_the_screen(sw, sh):
    w, h = _fit_size(sw, sh)
    assert w <= sw and h <= sh                 # never larger than the display (fits 13" Macs)
    assert w >= 900 and h >= 600               # never below the usable minimum
    assert w <= 1600 and h <= 1000             # capped so huge externals don't open absurd


def test_uses_most_of_a_13in_display():
    # On a 13" MBA it should be noticeably bigger than the old fixed 1100×760, not a token bump.
    w, h = _fit_size(1440, 900)
    assert w >= 1300 and h >= 800


def test_caps_on_a_huge_external():
    assert _fit_size(3840, 2160) == (1600, 1000)


# --- the browser fallback (a developer mode: the cookie reaches every other 127.0.0.1 port) -----

class _FakeServer:
    stopped = False

    def stop(self) -> None:
        self.stopped = True


def test_browser_mode_warns_before_printing_the_url(monkeypatch, capsys):
    monkeypatch.delattr(sys, "frozen", raising=False)

    def ctrl_c(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(app_module.time, "sleep", ctrl_c)
    server = _FakeServer()
    _serve_in_browser(server, "http://127.0.0.1:1/?token=x")
    out = capsys.readouterr().out
    assert "WARNING" in out and "session cookie" in out and "native window" in out
    assert out.index("WARNING") < out.index("?token=x")
    assert server.stopped


def test_the_packaged_app_refuses_browser_mode(monkeypatch, capsys):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    server = _FakeServer()
    with pytest.raises(SystemExit):
        _serve_in_browser(server, "http://127.0.0.1:1/?token=x")
    assert "token=x" not in capsys.readouterr().out  # the URL is never handed out
    assert server.stopped
