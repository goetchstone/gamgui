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


def main() -> None:
    # Optional Touch ID gate (fails open if unavailable; disable with GAMGUI_NO_BIOMETRICS=1).
    from .core.biometrics import require_unlock

    if not require_unlock("unlock GamGUI"):
        print("[GamGUI] Touch ID cancelled — exiting. (Set GAMGUI_NO_BIOMETRICS=1 to disable.)")
        return

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

    webview.create_window("GamGUI", url, width=1100, height=760, min_size=(900, 600))
    try:
        webview.start()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
