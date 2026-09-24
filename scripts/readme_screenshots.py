"""Regenerate the README screenshots from the mock-backed app — never from a real tenant.

    .venv/bin/python scripts/readme_screenshots.py            # writes docs/screenshots/*.png

Runs scripts/preview_mock.py's app (strict mock gam, fake example.com data) on port 8799 in a
background thread and drives headless Google Chrome over the DevTools protocol at 1280 px wide, 2x:
Users, the signature designer with a preview, a calendar's access, and an offboarding preview.
Needs Google Chrome installed; `websockets` comes with uvicorn[standard]. Screenshots once showed
the operator's company name and an internal calendar name because they were captured by hand.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import preview_mock  # noqa: E402

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "screenshots"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
W = 1280

FILL = """
(sel, val) => { const el = document.querySelector(sel); if (!el) throw new Error('no ' + sel);
  el.value = val; for (const t of ['input','keyup','change']) el.dispatchEvent(new Event(t, {bubbles:true})); return true; }
"""


class Tab:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def cmd(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr):
        r = await self.cmd("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"JS error: {r['exceptionDetails'].get('exception', {}).get('description')}")
        return r.get("result", {}).get("value")

    async def fill(self, sel, val):
        return await self.js(f"({FILL})({json.dumps(sel)}, {json.dumps(val)})")

    async def click(self, sel):
        return await self.js(f"(() => {{ const el = document.querySelector({json.dumps(sel)}); if (!el) throw new Error('no {sel}'); el.click(); return true; }})()")

    async def click_text(self, text, tag="button, a"):
        return await self.js(
            f"(() => {{ const el = [...document.querySelectorAll({json.dumps(tag)})].find(e => e.textContent.trim().startsWith({json.dumps(text)}));"
            f" if (!el) throw new Error('no element with text {text}'); el.click(); return true; }})()")

    async def wait_for(self, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if await self.js(f"!!({cond})"):
                await asyncio.sleep(0.6)   # let htmx settle + fonts paint
                return
            await asyncio.sleep(0.2)
        raise TimeoutError(cond)

    async def goto(self, path):
        await self.cmd("Page.navigate", url=BASE + path)
        await self.wait_for("document.readyState === 'complete' && document.fonts.status === 'loaded'")

    async def shot(self, name, min_h=880):
        h = await self.js("Math.ceil(document.documentElement.scrollHeight)")
        h = max(min_h, min(h, 2400))
        await self.cmd("Emulation.setDeviceMetricsOverride", width=W, height=h, deviceScaleFactor=2, mobile=False)
        await asyncio.sleep(0.5)
        r = await self.cmd("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        (OUT / name).write_bytes(base64.b64decode(r["data"]))
        await self.cmd("Emulation.setDeviceMetricsOverride", width=W, height=880, deviceScaleFactor=2, mobile=False)
        print("saved", name, W * 2, "x", h * 2)



def _serve(state_dir: Path) -> None:
    import uvicorn

    preview_mock.PORT = PORT
    os.environ["HOME"] = str(state_dir)
    for var in ("GAMCFGDIR", "XDG_DATA_HOME", "GAMGUI_GAM_BINARY"):
        os.environ.pop(var, None)
    os.environ["GAM_MOCK_FIXTURES"] = str(preview_mock.FIXTURES)
    app = preview_mock.build_app(state_dir)
    threading.Thread(target=uvicorn.run, args=(app,), kwargs={"host": "127.0.0.1", "port": PORT,
                     "log_level": "warning"}, daemon=True).start()
    for _ in range(60):
        try:
            req = urllib.request.Request(f"{BASE}/healthz", headers={"Host": f"127.0.0.1:{PORT}"})
            if urllib.request.urlopen(req).status == 200:
                return
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.25)
    raise SystemExit("the mock preview didn't start")


# Every headless Chrome this repo starts gets these. A fresh --user-data-dir makes macOS Chrome ask the
# login Keychain for its "Chrome Safe Storage" key (to encrypt the throwaway profile's cookies), which
# pops a Keychain prompt on the operator's screen for every run; --use-mock-keychain keeps it off the
# real Keychain. tests/test_headless_chrome.py fails on a launch without it.
CHROME_FLAGS = ["--headless=new", "--use-mock-keychain", "--no-first-run", "--no-default-browser-check",
                "--hide-scrollbars"]


async def shoot():
    prof = tempfile.mkdtemp(prefix="chrome-shots-")
    proc = subprocess.Popen([CHROME, *CHROME_FLAGS, "--remote-debugging-port=9333", f"--user-data-dir={prof}",
                             "--window-size=1280,880", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                pages = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json/list"))
                page = next(p for p in pages if p["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
            t = Tab(ws)
            await t.cmd("Page.enable")
            await t.cmd("Emulation.setDeviceMetricsOverride", width=W, height=880, deviceScaleFactor=2, mobile=False)
            await t.goto("/?token=t")                                   # sets the session cookie

            await t.goto("/users")
            await t.wait_for("document.querySelectorAll('tbody tr').length >= 3")
            await t.shot("users.png")

            await t.goto("/signatures")
            await t.js("(() => { const s = document.querySelector('select[name=scope_type]'); s.value = 'company';"
                       " s.dispatchEvent(new Event('change', {bubbles:true})); return true; })()")
            await t.fill("textarea[name=template]",
                         '<div style="font-family:Georgia,serif"><b>{name}</b><br>[[{title} · ]]Example Co.<br>'
                         '<a href="mailto:{email}">{email}</a>[[ · {phone}]]</div>')
            await t.click_text("Preview")
            await t.wait_for("document.querySelector('iframe') && /Applies to/.test(document.body.innerText)")
            await t.shot("signatures.png")

            await t.goto("/calendars")
            await t.fill("input[name=q]", "sales")
            await t.wait_for("/Sales Team/.test(document.body.innerText) && /View access/.test(document.body.innerText)")
            await t.click_text("View access")
            await t.wait_for("/WHO HAS ACCESS/i.test(document.body.innerText)")
            await t.shot("calendars.png")

            await t.goto("/lifecycle")
            await t.fill("input[name=user]", "carol@example.com")
            await t.fill("input[name=manager]", "alice@example.com")
            await t.click_text("Preview")
            await t.wait_for("/Run offboarding|Confirm|steps/i.test(document.body.innerText) && document.querySelectorAll('code, pre').length > 0", 15)
            await t.shot("lifecycle.png")
    finally:
        proc.terminate()



if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gamgui-shots-") as tmp:
        _serve(Path(tmp))
        asyncio.run(shoot())
