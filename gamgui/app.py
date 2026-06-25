"""Application entry point.

Starts the local FastAPI server on a random loopback port and opens it in a native WKWebView
window via pywebview. If pywebview isn't installed (e.g. headless dev), it prints the tokenized URL
and keeps serving so you can open it in a browser.
"""

from __future__ import annotations

import socket
import threading
import time

import uvicorn

from .web.server import AppState, create_app


def _free_loopback_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _BackgroundServer:
    def __init__(self, app, host: str, port: int) -> None:
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        # Block until uvicorn reports it has bound the socket.
        while not self.server.started:
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=5)


def _fit_size(screen_w: int, screen_h: int) -> "tuple[int, int]":
    """A window size that uses most of the display but always fits it — including 13" Macs.

    Leaves room for the menu bar/dock, caps the size on large external monitors so it never opens
    absurdly wide, respects the 900×600 minimum, and never exceeds the screen.
    """
    w = max(900, min(screen_w - 40, 1600))
    h = max(600, min(screen_h - 90, 1000))
    return min(w, screen_w), min(h, screen_h)


def main() -> None:
    state = AppState.create()
    app = create_app(state)
    host, port = "127.0.0.1", _free_loopback_port()
    server = _BackgroundServer(app, host, port)
    server.start()
    url = f"http://{host}:{port}/?token={state.token}"

    try:
        import webview  # pywebview (optional 'desktop' extra)
    except ImportError:
        print(f"[GamGUI] pywebview not installed — open this URL in a browser:\n  {url}")
        print("[GamGUI] (install the native window with: pip install '.[desktop]')  Ctrl-C to quit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
        return

    window = webview.create_window("GamGUI", url, width=1100, height=760, min_size=(900, 600))

    def _fit_to_screen() -> None:
        # Once the GUI loop knows the display, grow the window to fit it (so the full-width screens
        # have room) and center it. Best-effort — falls back silently to the default 1100×760.
        try:
            screens = list(getattr(webview, "screens", None) or [])
            if not screens:
                return
            sw, sh = int(screens[0].width), int(screens[0].height)
            w, h = _fit_size(sw, sh)
            window.resize(w, h)
            window.move(max(0, (sw - w) // 2), max(20, (sh - h) // 3))
        except Exception:
            pass

    try:
        webview.start(_fit_to_screen)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
