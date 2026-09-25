"""Accessibility ratchet: axe-core over every main screen of the mock-backed app, in headless Chrome.

    .venv/bin/python -m pytest -m a11y -rP                          # make a11y; CI's a11y job
    A11Y_UPDATE_BASELINE=1 .venv/bin/python -m pytest -m a11y       # after a fix: lower the baseline

Drives scripts/preview_mock.py's app (strict mock gam, fake example.com data, a temp $HOME) through
each screen and the states that matter (user-detail and onboarding tabs, the Groups board's combobox
and remove step, a signature preview, a calendar's access, a Builder result and a User slot's open
suggestions, an offboarding preview and its run), injects the vendored axe-core (tests/a11y/,
checksum-checked) and counts the serious/critical violations per screen and rule. It fails on a rule or
screen the baseline (tests/a11y/baseline.json) doesn't list, on a count above it — and on a count below
it, so a fix locks its gain in. The baseline only ever shrinks, to empty (it is).
A second Chrome test uses the keyboard alone (real key events): the ARIA tab strips, focus landing in a
confirm panel and coming back when it closes, and a brand focus ring on every Tab stop of every screen.
Another walks a Builder User slot's combobox by keys and a click. Another works the Groups board by keys
alone — find a group, add a person with a role through its combobox, remove a member through its
confirm step (plan A6). One goes Back from a user to the Users list and finds it as left, and opens
user-detail tabs to see each load on first open (plans U8, U13).
A last one runs an offboarding and
reads Chrome's accessibility tree: a polled panel speaks through base.html's one live region, which no
poll replaces (plan A3).

The Chrome runs are marked a11y and deselected from the default run (pyproject addopts: they start
Chrome); the checksum and ratchet-logic tests and the template checks (every field named, every focus
target focusable, every tab strip an ARIA tablist, every polled panel's line and marks) run by default. Skipped when Google Chrome
isn't installed, unless A11Y_REQUIRE_CHROME is set (CI). Chrome gets scripts/readme_screenshots.py's
CHROME_FLAGS — --use-mock-keychain keeps it off the login Keychain (tests/test_headless_chrome.py) —
and talks CDP over --remote-debugging-pipe: no debugging port for another local process to reach, no
websocket client to install.
"""

from __future__ import annotations

import fcntl
import functools
import hashlib
import importlib.util
import json
import os
import re
import select
import signal
import socket
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
A11Y = ROOT / "tests" / "a11y"
BASELINE = A11Y / "baseline.json"
GATED = ("serious", "critical")
W, H = 1100, 760   # the app window


@functools.cache
def _shots():
    """scripts/readme_screenshots.py: CHROME, CHROME_FLAGS, and the preview_mock it imports."""
    spec = importlib.util.spec_from_file_location("readme_screenshots", ROOT / "scripts" / "readme_screenshots.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _axe_source() -> str:
    """The vendored axe.min.js, refused unless it matches its recorded SHA-256."""
    expected = (A11Y / "axe.min.js.sha256").read_text().split()[0]
    data = (A11Y / "axe.min.js").read_bytes()
    assert hashlib.sha256(data).hexdigest() == expected, "tests/a11y/axe.min.js doesn't match axe.min.js.sha256"
    return data.decode()


# A focus indicator is base.html's brand ring (a solid brand-blue outline; Chrome's own blue one doesn't
# count) or a text field's focus:ring (a box-shadow; its focus:outline-none leaves a transparent outline).
FOCUSED = """(() => { const a = document.activeElement; if (!a || a === document.body) return null;
  const s = getComputedStyle(a);
  return {id: a.id, tag: a.tagName, text: a.textContent.trim().replace(/\\s+/g, ' ').slice(0, 50),
          html: a.outerHTML.slice(0, 160), panel: a.hasAttribute('data-focus'),
          ring: (s.outlineStyle === 'solid' && s.outlineColor === 'rgb(90, 114, 142)') || s.boxShadow !== 'none'};
})()"""

ERROR_TRAP = """window.__errs = [];
addEventListener('error', e => window.__errs.push(String(e.message)));
addEventListener('unhandledrejection', e => window.__errs.push(String(e.reason)));"""

# axe.run's defaults (WCAG A/AA and best practices) minus iframes: the only frame is the sandboxed
# signature preview, which holds the operator's email HTML, not this UI, and can't run axe anyway.
AXE_RUN = """
axe.run(document, {iframes: false, resultTypes: ['violations']}).then(r =>
  r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length,
                          targets: v.nodes.slice(0, 3).map(n => n.target.join(' '))})))
"""


class Chrome:
    """Headless Chrome over --remote-debugging-pipe: CDP as NUL-terminated JSON on the child's fds 3
    (commands in) and 4 (replies and events out)."""

    def __init__(self, profile: Path):
        cmd_r, self._w = os.pipe()
        self._r, out_w = os.pipe()
        # The child's ends must land on exactly 3 and 4. Parked at >= 10 first, neither dup2 can
        # overwrite the other's source (or be a no-op that leaves close-on-exec set).
        child = [fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10) for fd in (cmd_r, out_w)]
        os.close(cmd_r)
        os.close(out_w)
        chrome = _shots().CHROME
        argv = [chrome, *_shots().CHROME_FLAGS, "--remote-debugging-pipe", f"--user-data-dir={profile}",
                f"--window-size={W},{H}", "about:blank"]
        try:
            self.pid = os.posix_spawn(chrome, argv, os.environ, file_actions=[
                (os.POSIX_SPAWN_DUP2, child[0], 3), (os.POSIX_SPAWN_DUP2, child[1], 4),
                (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0)])
        finally:
            for fd in child:
                os.close(fd)
        self._buf, self._n, self._session = b"", 0, None
        try:
            target = self.cmd("Target.createTarget", url="about:blank")["targetId"]
            self._session = self.cmd("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
            self.cmd("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
        except BaseException:
            self.close()
            raise

    def cmd(self, method: str, timeout: float = 30, **params):
        self._n += 1
        msg = {"id": self._n, "method": method, "params": params}
        if self._session and not method.startswith(("Target.", "Browser.")):
            msg["sessionId"] = self._session
        data = json.dumps(msg).encode() + b"\0"
        while data:
            data = data[os.write(self._w, data):]
        end = time.monotonic() + timeout
        while True:
            while b"\0" not in self._buf:
                if not select.select([self._r], [], [], max(0.0, end - time.monotonic()))[0]:
                    raise TimeoutError(method)
                chunk = os.read(self._r, 1 << 20)
                if not chunk:
                    raise RuntimeError("Chrome closed the DevTools pipe")
                self._buf += chunk
            raw, _, self._buf = self._buf.partition(b"\0")
            reply = json.loads(raw)
            if reply.get("id") == self._n:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})

    def js(self, expr: str, timeout: float = 30):
        r = self.cmd("Runtime.evaluate", timeout=timeout, expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            detail = r["exceptionDetails"]
            raise RuntimeError(f"JS error: {detail.get('exception', {}).get('description') or detail.get('text')}")
        return r.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.cmd("Browser.close", timeout=5)
        except (OSError, RuntimeError, TimeoutError):
            pass
        for _ in range(50):
            if os.waitpid(self.pid, os.WNOHANG)[0]:
                break
            time.sleep(0.1)
        else:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        os.close(self._r)
        os.close(self._w)


class Page:
    """One tab of the app: navigate, act like an operator, wait for htmx to settle, run axe."""

    def __init__(self, chrome: Chrome, base: str, axe: str):
        self.c, self.base, self.axe = chrome, base, axe

    def wait(self, cond: str, timeout: float = 15) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.c.js(f"!!({cond})"):
                return
            time.sleep(0.15)
        raise TimeoutError(f"{cond!r} on {self.c.js('location.pathname')}: "
                           f"{(self.c.js('document.body && document.body.innerText') or '')[:600]}")

    def settle(self, cond: str = "true", timeout: float = 15) -> None:
        """`cond` holds, the document is loaded, and no htmx request is in flight twice in a row."""
        ready = f"document.readyState === 'complete' && !document.querySelector('.htmx-request') && ({cond})"
        self.wait(ready, timeout)
        time.sleep(0.3)
        self.wait(ready, timeout)

    def goto(self, path: str, cond: str = "true") -> None:
        self.c.js("window.__a11yOld = true")
        self.c.cmd("Page.navigate", url=self.base + path)
        self.wait("!window.__a11yOld && window.htmx")
        self.settle(cond)

    def fill(self, sel: str, val: str) -> None:
        self.c.js(f"""(() => {{ const el = document.querySelector({json.dumps(sel)});
            if (!el) throw new Error('no ' + {json.dumps(sel)}); el.value = {json.dumps(val)};
            for (const t of ['input', 'keyup', 'change']) el.dispatchEvent(new Event(t, {{bubbles: true}})); }})()""")

    def click(self, sel: str, text: str | None = None, action: str = "click") -> None:
        """Click (or `action`, e.g. focus) the first `sel` whose trimmed text starts with `text`, if given."""
        self.c.js(f"""(() => {{ const el = [...document.querySelectorAll({json.dumps(sel)})]
            .find(e => {json.dumps(text)} === null || e.textContent.trim().startsWith({json.dumps(text)}));
            if (!el) throw new Error('no ' + {json.dumps(sel)} + ' ' + {json.dumps(text)}); el.{action}(); }})()""")

    KEYS = {"Tab": 9, "Enter": 13, "Escape": 27, "End": 35, "Home": 36, "ArrowLeft": 37, "ArrowUp": 38,
            "ArrowRight": 39, "ArrowDown": 40}

    def key(self, name: str) -> None:
        """Press a key the way the keyboard does (CDP input events: Tab moves focus, Enter activates, a
        letter types — on a closed <select>, it picks the option it starts)."""
        if len(name) == 1:
            ev = {"key": name, "code": f"Key{name.upper()}", "windowsVirtualKeyCode": ord(name.upper())}
            down = {"type": "keyDown", "text": name}
        else:
            ev = {"key": name, "code": name, "windowsVirtualKeyCode": self.KEYS[name]}
            down = {"type": "keyDown", "text": "\r"} if name == "Enter" else {"type": "rawKeyDown"}
        self.c.cmd("Input.dispatchKeyEvent", **down, **ev)
        self.c.cmd("Input.dispatchKeyEvent", type="keyUp", **ev)

    def type(self, text: str) -> None:
        """Type into the focused field (one input event, as a paste or an IME commit makes)."""
        self.c.cmd("Input.insertText", text=text)

    def focused(self) -> dict:
        """What has focus: its id, text, whether it is a [data-focus] panel and shows a focus indicator."""
        return self.c.js(FOCUSED) or {}

    def violations(self) -> list[dict]:
        if not self.c.js("!!window.axe"):
            self.c.js(self.axe)
        return [v for v in self.c.js(AXE_RUN, timeout=60) if v["impact"] in GATED]


OFFBOARD_DONE = ("/Offboarding (complete|incomplete|stopped|interrupted)/"
                 ".test(document.querySelector('#offboard-result').innerText)")


def _offboard_preview(p: Page) -> None:
    """An offboarding of carol@example.com previewed, its Run's confirm() answered yes."""
    p.goto("/lifecycle")
    p.fill("input[name=user]", "carol@example.com")
    p.fill("input[name=manager]", "alice@example.com")
    p.click("button", "Preview")
    p.settle("document.querySelector('#offboard-result code, #offboard-result pre')")
    p.c.js("window.confirm = () => true")          # Run offboarding's hx-confirm


def _screens(p: Page):
    """Put the app in each state worth checking; yield its name there."""
    p.goto("/")
    yield "home"

    p.goto("/users", "document.querySelectorAll('#users-table tbody tr').length >= 3")
    yield "users"

    p.goto("/users/detail?email=alice%40example.com")
    for tab in ("overview", "mail", "sharing", "danger"):
        p.click(f"#tab-{tab}")
        p.settle()
        yield f"user-detail/{tab}"

    p.goto("/groups", "document.querySelector('#group-results [data-group]')")
    p.click("#group-results [data-group]")                  # the first group: its members and the add form
    p.settle("document.querySelector('#member-list table')")
    yield "groups"
    p.click("#member-email", action="focus")
    p.fill("#member-email", "a")
    p.settle("document.querySelector('#people-results [role=option]')")
    p.key("ArrowDown")
    yield "groups/suggestions"                              # the add field's combobox, open, one option active (A6)
    p.click("#member-list button", "Remove")
    p.settle("document.querySelector('#member-confirm [data-focus]')")
    yield "groups/remove"

    p.goto("/signatures")
    p.c.js("(() => { const s = document.querySelector('select[name=scope_type]'); s.value = 'company';"
           " s.dispatchEvent(new Event('change', {bubbles: true})); })()")
    p.fill("textarea[name=template]", "<b>{name}</b><br>[[{title} · ]]Example Co.<br>{email}")
    p.click("button", "Preview")
    p.settle("document.querySelector('iframe') && /Applies to/.test(document.body.innerText)")
    yield "signatures"

    p.goto("/calendars")
    p.fill("input[name=q]", "sales")
    p.settle("/View access/.test(document.body.innerText)")
    p.click("button, a", "View access")
    p.settle("/who has access/i.test(document.body.innerText)")
    yield "calendars"

    p.goto("/builder", "document.querySelector('#catalog button')")
    p.fill("#cat-controls input[name=q]", "print groups")
    p.settle("/List groups\\./.test(document.querySelector('#catalog').innerText)")
    p.click("#catalog button", "Build")                        # the first hit: gam print groups
    p.settle("document.querySelector('#cmd-form form')")
    p.click("#cmd-form button", "Preview")
    p.settle("document.querySelector('#builder-result button')")
    p.click("#builder-result button", "Run")
    p.settle("document.querySelector('#builder-result table')")
    yield "builder"
    p.c.js("htmx.ajax('GET', '/builder/command/build.add_delegate', {target: '#cmd-form', swap: 'innerHTML'})")
    p.settle("document.getElementById('slot-email')")
    p.click("#slot-email", action="focus")
    p.settle("document.getElementById('slot-email').getAttribute('aria-expanded') === 'true'")
    p.key("ArrowDown")
    yield "builder/suggestions"                             # a User slot's combobox, open, one option active

    p.goto("/onboard")
    for tab in ("generate", "roles", "welcome", "bulk"):
        p.click(f"#tab-{tab}")
        p.settle()
        yield f"onboard/{tab}"

    _offboard_preview(p)
    yield "lifecycle"
    p.click("button", "Run offboarding")
    p.settle(OFFBOARD_DONE, timeout=60)
    yield "lifecycle/run"                          # the finished panel and its ✓/✗ log (plan A3)
    p.click("#jobs-toggle")
    p.settle("!document.getElementById('jobs-panel').hidden && document.querySelector('#jobs-list a')")
    yield "jobs/tray"                              # the header's tray, open, listing that run (plan U5)
    p.goto(p.c.js("document.querySelector('#jobs-list a').getAttribute('href')"),
           "document.querySelector('#job-panel [data-announce]')")
    yield "jobs/page"                              # the run's own panel, on a page of its own

    p.goto("/reports", "!/Loading/.test(document.querySelector('#usage')?.innerText ?? 'Loading')")
    yield "reports"

    p.goto("/audit", "!/Loading/.test(document.querySelector('#audit-rows')?.innerText ?? 'Loading')")
    yield "audit"

    p.goto("/setup")
    yield "setup"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app_url(tmp_path_factory):
    import uvicorn

    preview_mock = _shots().preview_mock
    state = tmp_path_factory.mktemp("a11y-app")
    port = _free_port()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(state))      # the Setup screen's scan stays out of the real ~/.gam
        for var in ("GAMCFGDIR", "XDG_DATA_HOME", "GAMGUI_GAM_BINARY"):
            mp.delenv(var, raising=False)
        mp.setenv("GAM_MOCK_FIXTURES", str(preview_mock.FIXTURES))
        mp.setattr(preview_mock, "PORT", port)
        server = uvicorn.Server(uvicorn.Config(preview_mock.build_app(state), host="127.0.0.1", port=port,
                                               log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("the mock-backed app didn't start")
        yield f"http://127.0.0.1:{port}"
        server.should_exit = True
        thread.join(10)


@pytest.fixture(scope="module")
def page(app_url, tmp_path_factory):
    chrome_bin = _shots().CHROME
    if not Path(chrome_bin).exists():
        if os.environ.get("A11Y_REQUIRE_CHROME"):
            pytest.fail(f"A11Y_REQUIRE_CHROME is set but there is no {chrome_bin}")
        pytest.skip("Google Chrome isn't installed")
    chrome = Chrome(tmp_path_factory.mktemp("a11y-chrome"))
    try:
        p = Page(chrome, app_url, _axe_source())
        p.goto(f"/?token={_shots().preview_mock.TOKEN}")      # sets the session cookie
        yield p
    finally:
        chrome.close()


def _summary(found: dict[str, list[dict]]) -> str:
    lines = [f"  {screen:<21} {v['id']:<17} {v['impact']:<8} x{v['nodes']:<3} {v['help']} — e.g. "
             + " | ".join(v["targets"])
             for screen, vs in found.items() for v in sorted(vs, key=lambda v: (-v["nodes"], v["id"]))]
    return "\n".join(lines) or "  (none)"


def _ratchet(counts: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]) -> tuple[list[str], list[str]]:
    """(worse, better): a line per screen/rule whose node count rose above, or fell below, the baseline's."""
    now = {(s, r): n for s, c in counts.items() for r, n in c.items()}
    was = {(s, r): n for s, c in baseline.items() for r, n in c.items()}
    lines = [(now.get(k, 0) - was.get(k, 0), f"{k[0]} {k[1]}: {now.get(k, 0)} (baseline {was.get(k, 0)})")
             for k in sorted(now.keys() | was.keys())]
    return [line for d, line in lines if d > 0], [line for d, line in lines if d < 0]


@pytest.mark.a11y
@pytest.mark.timeout(300)
def test_no_new_serious_or_critical_axe_violations(page):
    found = {screen: page.violations() for screen in _screens(page)}
    counts = {screen: {v["id"]: v["nodes"] for v in vs} for screen, vs in found.items() if vs}
    print(f"axe serious/critical violations, {sum(map(len, found.values()))} rule hits:\n{_summary(found)}")

    if os.environ.get("A11Y_UPDATE_BASELINE"):
        BASELINE.write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n")
        return
    worse, better = _ratchet(counts, json.loads(BASELINE.read_text()))
    assert not worse, "new or more serious/critical accessibility violations:\n  " + "\n  ".join(worse)
    assert not better, ("fewer violations than tests/a11y/baseline.json — lock the gain in with "
                        "A11Y_UPDATE_BASELINE=1:\n  " + "\n  ".join(better))


# Screens whose every Tab stop must show a focus indicator (plan A5).
RING_WALK = ("/", "/users", "/users/detail?email=alice%40example.com", "/groups", "/signatures", "/calendars",
             "/builder", "/onboard", "/lifecycle", "/reports", "/audit", "/setup")


@pytest.mark.a11y
@pytest.mark.timeout(300)
def test_the_keyboard_alone_works_the_tabs_and_each_confirm_panel(page):
    """Plan A2 and A5, driven by real key events: the tab strips are ARIA tabs (one Tab stop, arrows, Home,
    End), a confirm panel takes focus when it opens and hands it back when it closes, every Tab stop shows
    a focus ring, and no page throws."""
    p = page
    p.c.cmd("Page.enable")
    p.c.cmd("Page.addScriptToEvaluateOnNewDocument", source=ERROR_TRAP)

    def inside(zone: str) -> bool:
        return p.c.js(f"document.getElementById({json.dumps(zone)}).contains(document.activeElement)")

    def shown() -> tuple[list[str], list[str]]:
        return (p.c.js("[...document.querySelectorAll('[role=tab][aria-selected=true]')].map(t => t.id)"),
                p.c.js("[...document.querySelectorAll('[role=tabpanel]')].filter(el => el.offsetParent).map(el => el.id)"))

    p.goto("/users/detail?email=alice%40example.com")
    p.click('main a[href="/users"]', action="focus")
    p.key("Tab")                          # the directory's "as of … · Refresh" link sits under the name
    assert "refresh=1" in p.c.js("document.activeElement.getAttribute('href') || ''")
    p.key("Tab")
    assert p.focused()["id"] == "tab-overview"
    for key, tab in (("ArrowRight", "mail"), ("End", "danger"), ("Home", "overview"), ("ArrowLeft", "danger")):
        p.key(key)
        assert p.focused()["id"] == f"tab-{tab}", key
        assert shown() == ([f"tab-{tab}"], [f"panel-{tab}"]), key
    p.key("Tab")
    assert p.focused()["id"] == "panel-danger"          # the strip was one stop: roving tabindex

    # Suspend… lands on its confirm panel; Cancel re-renders the zone and focus returns to Suspend….
    p.key("Tab")
    assert p.focused()["text"] == "Suspend…"
    p.key("Enter")
    p.settle("document.querySelector('#suspend-zone [data-focus]')")
    assert p.focused()["panel"] and inside("suspend-zone")
    p.key("Tab")
    p.key("Tab")
    assert p.focused()["text"] == "Cancel"
    p.key("Enter")
    p.settle("!document.querySelector('#suspend-zone [data-focus]')")
    assert p.focused()["text"] == "Suspend…"

    # The account delete lands in its typed confirmation, and Cancel returns to Delete account….
    p.key("Tab")
    p.key("Tab")
    assert p.focused()["text"] == "Delete account…"
    p.key("Enter")
    p.settle("document.querySelector('#del-confirm-email')")
    assert p.focused()["id"] == "del-confirm-email"
    p.key("Tab")
    p.key("Tab")
    p.key("Enter")
    p.settle("!document.querySelector('#del-confirm-email')")
    assert p.focused()["text"] == "Delete account…"
    assert p.c.js("window.__errs") == []

    # A calendar delete's Cancel only empties its zone (the "clear" action): focus returns to its opener.
    p.goto("/calendars")
    p.fill("input[name=q]", "sales")
    p.settle("/View access/.test(document.body.innerText)")
    p.click("button, a", "View access")
    p.settle("/who has access/i.test(document.body.innerText)")
    p.click("#cal-detail button", "Delete this calendar", action="focus")
    p.key("Enter")
    p.settle("document.querySelector('#cal-delete-zone [data-focus]')")
    assert p.focused()["text"] == "Permanently delete this calendar?"
    for _ in range(3):
        p.key("Tab")                                    # the typed DELETE, Delete calendar, Cancel
    assert p.focused()["text"] == "Cancel"
    p.key("Enter")
    assert p.focused()["text"] == "Delete this calendar…"
    assert p.c.js("document.getElementById('cal-delete-zone').children.length") == 0

    # A Builder preview lands on its panel; Run replaces it, and focus stays on the result's zone.
    p.goto("/builder", "document.querySelector('#catalog button')")
    p.fill("#cat-controls input[name=q]", "print groups")
    p.settle("/List groups\\./.test(document.querySelector('#catalog').innerText)")
    p.click("#catalog button", "Build")
    p.settle("document.querySelector('#cmd-form form')")
    p.click("#cmd-form button", "Preview", action="focus")
    p.key("Enter")
    p.settle("document.querySelector('#builder-result [data-focus]')")
    assert p.focused()["panel"] and inside("builder-result")
    p.key("Tab")
    assert p.focused()["text"] == "Run"
    p.key("Enter")
    p.settle("document.querySelector('#builder-result table')")
    assert p.focused()["id"] == "builder-result"
    assert p.c.js("window.__errs") == []

    # Onboarding's strip too.
    p.goto("/onboard")
    p.click("#tab-generate", action="focus")
    p.key("ArrowRight")
    assert shown() == (["tab-roles"], ["panel-roles"])

    # Every Tab stop on every screen shows a focus indicator.
    bare, stops = [], {}
    for path in RING_WALK:
        p.goto(path)
        p.c.js("document.activeElement && document.activeElement.blur()")
        seen: list[str] = []
        for _ in range(150):
            p.key("Tab")
            f = p.focused()
            if not f or f["html"] in seen[:1]:       # past the last stop, or wrapped round to the first
                break
            seen.append(f["html"])
            if not f["ring"]:
                bare.append(f"{path}: {f['html']}")
        stops[path] = len(seen)
        assert p.c.js("window.__errs") == [], path
    print("Tab stops per screen:", stops)
    assert min(stops.values()) > 11, stops                 # past the wordmark and the ten nav links
    assert not bare, "Tab stops with no visible focus indicator:\n  " + "\n  ".join(bare)


@pytest.mark.a11y
@pytest.mark.timeout(120)
def test_the_jobs_tray_works_from_the_keyboard(page):
    """Plan U5: the header's Jobs button is a disclosure — Enter opens it, Tab walks its rows (each a link
    to the job's own page), Escape closes it and hands focus back, and a click outside closes it."""
    p = page
    p.c.cmd("Page.enable")
    p.c.cmd("Page.addScriptToEvaluateOnNewDocument", source=ERROR_TRAP)
    _offboard_preview(p)
    p.click("button", "Run offboarding")
    p.settle(OFFBOARD_DONE, timeout=60)

    def expanded() -> tuple:
        return (p.c.js("document.getElementById('jobs-toggle').getAttribute('aria-expanded')"),
                p.c.js("!document.getElementById('jobs-panel').hidden"))

    p.goto("/users")
    p.click("#jobs-toggle", action="focus")
    p.key("Enter")
    assert expanded() == ("true", True)
    p.key("Tab")
    link = p.focused()
    assert link["tag"] == "A" and link["id"].startswith("job-link-") and link["ring"]
    p.key("Escape")
    assert expanded() == ("false", False) and p.focused()["id"] == "jobs-toggle"
    p.key("Enter")
    p.c.js("document.querySelector('main').click()")
    assert expanded() == ("false", False)
    p.key("Enter")
    p.key("Tab")
    p.c.js("window.__a11yOld = true")
    p.key("Enter")                                          # follow the link to the job's page
    p.wait("!window.__a11yOld && window.htmx")
    p.settle("document.querySelector('#job-panel [data-announce]')")
    assert re.match(r"/jobs/[\w-]+$", p.c.js("location.pathname"))
    assert "Offboarding complete" in p.c.js("document.getElementById('job-panel').innerText")
    assert p.c.js("window.__errs") == []


@pytest.mark.a11y
@pytest.mark.timeout(120)
def test_the_groups_board_works_from_the_keyboard(page):
    """Plan A6 and U7, by real key events: find a group, add a person with a role through the combobox,
    and remove a member through its confirm step — no drag, no mouse — while Chrome's accessibility tree
    names the combobox, its options and the member table's columns, and nothing throws."""
    p = page
    p.c.cmd("Page.enable")
    p.c.cmd("Page.addScriptToEvaluateOnNewDocument", source=ERROR_TRAP)

    def ax(role: str) -> list[str]:
        return [n.get("name", {}).get("value", "") for n in p.c.cmd("Accessibility.getFullAXTree")["nodes"]
                if not n.get("ignored") and n.get("role", {}).get("value") == role]

    def field(attr: str):
        return p.c.js(f"document.getElementById('member-email').{attr}")

    p.goto("/groups", "document.querySelector('#group-results [data-group]')")
    p.click("#group-q", action="focus")
    p.type("staff")
    p.settle("document.querySelectorAll('#group-results [data-group]').length === 1")
    p.key("Tab")
    assert p.focused()["text"].startswith("Staff")
    p.key("Enter")                                            # the group's panel takes focus at its heading
    p.settle("document.querySelector('#member-list table')")
    assert p.focused()["panel"] and p.focused()["text"] == "Staff"
    assert p.c.js("document.querySelector('#group-results [aria-current=true]').dataset.group") == "staff@example.com"
    assert {"Member", "Role"} <= set(ax("columnheader"))

    p.key("Tab")
    assert p.focused()["id"] == "member-email"
    p.type("car")
    p.wait("document.getElementById('member-email').getAttribute('aria-expanded') === 'true'")
    assert any(name.startswith("Add a person") for name in ax("combobox"))
    assert any("carol@example.com" in name for name in ax("option"))
    assert "suggestion" in p.c.js("document.getElementById('live-status').textContent")
    p.key("ArrowDown")
    assert field("getAttribute('aria-activedescendant')") == "person-1"
    p.key("Enter")                                            # takes the suggestion, doesn't submit
    assert field("value") == "carol@example.com" and field("getAttribute('aria-expanded')") == "false"
    p.key("Tab")
    p.key("o")                                                # the role select: Owner
    assert p.c.js("document.querySelector('#member-add [name=role]').value") == "owner"
    p.key("Tab")
    assert p.focused()["text"] == "Add"
    p.key("Enter")
    p.settle("document.querySelector('#member-list [data-added]')")
    said = "Added carol@example.com to staff@example.com as owner."
    assert p.c.js("document.getElementById('live-status').textContent") == said
    assert p.focused()["id"] == "member-email" and field("value") == ""      # ready for the next person

    p.click("#member-list button", "Remove…", action="focus")   # the first row: bob, a manager
    p.key("Enter")
    p.settle("document.querySelector('#member-confirm [data-focus]')")
    assert p.focused()["panel"] and p.focused()["text"] == "Remove bob@example.com from staff@example.com?"
    p.key("Tab")
    p.key("Tab")
    assert p.focused()["text"] == "Cancel"
    p.key("Enter")                                            # Cancel: the zone empties, focus goes back
    assert p.focused()["text"] == "Remove…" and p.c.js("document.getElementById('member-confirm').children.length") == 0
    p.key("Enter")
    p.settle("document.querySelector('#member-confirm [data-focus]')")
    p.key("Tab")
    assert p.focused()["text"] == "Remove"
    p.key("Enter")
    p.settle("/Removed bob@example.com/.test(document.getElementById('member-list').innerText)")
    assert p.focused()["panel"] and p.focused()["text"] == "Removed bob@example.com from staff@example.com."
    assert p.c.js("window.__errs") == []


@pytest.mark.a11y
@pytest.mark.timeout(120)
def test_the_builders_address_picker_is_a_combobox(page):
    """A Builder User/Group slot, by real key events: typing opens its suggestions, ArrowDown/Up move the
    active option, Enter takes it without submitting, Escape closes, ArrowDown reopens — while Chrome's
    accessibility tree names the combobox and its options — and a click still picks."""
    p = page
    p.c.cmd("Page.enable")
    p.c.cmd("Page.addScriptToEvaluateOnNewDocument", source=ERROR_TRAP)

    def ax(role: str) -> list[dict]:
        return [n for n in p.c.cmd("Accessibility.getFullAXTree")["nodes"]
                if not n.get("ignored") and n.get("role", {}).get("value") == role]

    def name(n: dict) -> str:
        return n.get("name", {}).get("value", "")

    def field(attr: str):
        return p.c.js(f"document.getElementById('slot-email').{attr}")

    p.goto("/builder", "document.querySelector('#catalog button')")
    p.c.js("htmx.ajax('GET', '/builder/command/build.add_delegate', {target: '#cmd-form', swap: 'innerHTML'})")
    p.settle("document.getElementById('slot-email')")
    p.click("#slot-email", action="focus")
    p.type("example")                                         # focus opened the list; typing narrows it and says so
    p.wait("/suggestion/.test(document.getElementById('live-status').textContent)")
    box = [n for n in ax("combobox") if name(n).startswith("User")]
    assert box, [name(n) for n in ax("combobox")]
    props = {q["name"]: q.get("value", {}).get("value") for q in box[0].get("properties", [])}
    assert props.get("expanded") is True and props.get("autocomplete") == "list"
    assert field("getAttribute('aria-controls')") == "slot-email-listbox"
    assert p.c.js("document.getElementById('slot-email-listbox').getAttribute('role')") == "listbox"
    assert any("alice@example.com" in name(n) for n in ax("option"))

    p.key("ArrowDown")
    assert field("getAttribute('aria-activedescendant')") == "slot-email-listbox-0"
    p.key("ArrowDown")
    assert field("getAttribute('aria-activedescendant')") == "slot-email-listbox-1"
    p.key("ArrowUp")
    assert field("getAttribute('aria-activedescendant')") == "slot-email-listbox-0"
    assert p.c.js("document.getElementById('slot-email-listbox-0').getAttribute('aria-selected')") == "true"
    picked = p.c.js("document.getElementById('slot-email-listbox-0').dataset.val")
    p.key("Enter")                                            # takes the suggestion, doesn't submit
    assert field("value") == picked and field("getAttribute('aria-expanded')") == "false"
    assert p.focused()["id"] == "slot-email" and not p.c.js("document.querySelector('#builder-result *')")

    p.key("ArrowDown")                                        # reopens the list
    p.wait("document.getElementById('slot-email').getAttribute('aria-expanded') === 'true'")
    p.key("Escape")
    assert field("getAttribute('aria-expanded')") == "false" and field("hasAttribute('aria-activedescendant')") is False
    assert p.c.js("document.querySelector('#slot-email').closest('.upick-wrap')"
                  ".querySelector('.upick-menu').classList.contains('hidden')")

    p.key("ArrowDown")
    p.wait("document.getElementById('slot-email').getAttribute('aria-expanded') === 'true'")
    p.key("Tab")                                              # leaving the field closes its list
    assert p.focused()["id"] == "slot-delegate" and field("getAttribute('aria-expanded')") == "false"

    p.c.js("document.getElementById('slot-email').value = ''")
    p.click("#slot-email", action="focus")                    # the whole list; the mouse still picks
    p.wait("document.getElementById('slot-email-listbox-1')")
    want = p.c.js("document.getElementById('slot-email-listbox-1').dataset.val")
    r = p.c.js("(() => { const b = document.getElementById('slot-email-listbox-1').getBoundingClientRect();"
               " return [b.left + 20, b.top + b.height / 2]; })()")
    for kind in ("mousePressed", "mouseReleased"):
        p.c.cmd("Input.dispatchMouseEvent", type=kind, x=r[0], y=r[1], button="left", clickCount=1)
    assert field("value") == want and want != picked and field("getAttribute('aria-expanded')") == "false"
    assert p.focused()["id"] == "slot-email"
    assert p.c.js("window.__errs") == []


@pytest.mark.a11y
@pytest.mark.timeout(120)
def test_back_from_a_user_reopens_the_list_as_left(page):
    """Plans U8 and U13 in the browser: a search and a sort (by keys) ride in the URL, so Back from a user
    and the detail page's "← Users" both reopen the list as it was left; the open tab is the #hash, and a
    tab's GAM reads load the first time it opens — the Overview asks for none."""
    p = page
    p.c.cmd("Page.enable")
    p.c.cmd("Page.addScriptToEvaluateOnNewDocument", source=ERROR_TRAP)
    view = "?q=staff&sort=email&desc=1"

    def emails() -> list[str]:
        return p.c.js("[...document.querySelectorAll('#users-table tbody td:nth-child(2)')].map(td => td.textContent.trim())")

    def fetched(path: str) -> bool:
        return p.c.js(f"performance.getEntriesByType('resource').some(e => new URL(e.name).pathname === {json.dumps(path)})")

    def the_list_as_left() -> None:
        p.settle("location.pathname === '/users' && document.querySelectorAll('#users-table tbody tr').length === 2")
        assert p.c.js("location.search") == view
        assert p.c.js("document.querySelector('input[name=q]').value") == "staff"
        assert emails() == ["bob@example.com", "alice@example.com"]
        assert p.c.js("document.querySelector('th[aria-sort]').textContent.trim()") == "Email▼"

    p.goto("/users", "document.querySelectorAll('#users-table tbody tr').length === 3")
    p.fill("input[name=q]", "staff")                          # matches the org unit /Staff: Alice and Bob
    p.settle("location.search === '?q=staff' && document.querySelectorAll('#users-table tbody tr').length === 2")
    p.click("#sort-email", action="focus")
    for want in ("?q=staff&sort=email", view):                # Enter sorts by email, Enter again reverses it
        p.key("Enter")
        p.settle(f"location.search === {json.dumps(want)}")
        assert p.focused()["id"] == "sort-email"               # htmx hands focus to the re-rendered header
    the_list_as_left()

    p.click("#users-table a", "Bob Brown")
    p.settle("location.pathname === '/users/detail'")
    assert p.c.js("document.querySelector('main a').getAttribute('href')") == "/users" + view
    assert not any(fetched(f"/users/{x}") for x in ("delegates", "vacation", "signature/current", "groups", "calendar"))
    p.click("#tab-mail")
    p.settle("!/Loading/.test(document.getElementById('panel-mail').innerText)")
    assert p.c.js("location.hash") == "#mail"
    assert all(fetched(f"/users/{x}") for x in ("delegates", "vacation", "signature/current"))
    assert not fetched("/users/groups") and not fetched("/users/calendar")

    p.c.js("history.back()")
    the_list_as_left()
    p.c.js("history.forward()")
    p.settle("location.pathname === '/users/detail' && !/Loading/.test(document.getElementById('panel-mail').innerText)")
    assert p.c.js("location.hash") == "#mail"
    assert p.c.js("document.querySelector('[role=tab][aria-selected=true]').id") == "tab-mail"
    p.click("main a", "← Users")
    the_list_as_left()

    p.goto("/users/detail?email=alice%40example.com#sharing")     # a fresh load opens the #hash's tab
    p.settle("!/Loading/.test(document.getElementById('panel-sharing').innerText)")
    assert p.c.js("document.querySelector('[role=tab][aria-selected=true]').id") == "tab-sharing"
    assert fetched("/users/groups") and fetched("/users/calendar") and not fetched("/users/delegates")
    assert p.c.js("window.__errs") == []


@pytest.mark.a11y
@pytest.mark.timeout(90)
def test_a_swap_that_replaces_the_focused_control_puts_focus_back(page):
    """Plan A-left2: a form that swaps itself out (title & department) or a list re-rendered under its own
    button (a delegate's Remove) took the focused control with it, and focus fell to <body>. app.js puts it
    on the control's re-rendered twin, or, when that is gone, on the zone, which opens with the result."""
    p = page
    p.c.cmd("Page.enable")
    p.c.cmd("Page.addScriptToEvaluateOnNewDocument", source=ERROR_TRAP)
    p.goto("/users/detail?email=carol%40example.com")
    p.click("form[hx-post='/users/organization'] button[type=submit]", action="focus")
    p.key("Enter")
    p.settle("document.getElementById('org-saved')")
    assert p.focused()["text"] == "Save title & department"
    assert p.c.js("document.activeElement.getAttribute('aria-describedby')") == "org-saved"   # "Saved ✓"

    p.click("#tab-mail")
    p.settle("document.querySelector('#delegates button[hx-post]')")
    assert p.c.js("document.querySelector('#delegates button[hx-post]').textContent") == "Remove helpdesk@example.com"
    p.c.js("window.confirm = () => true")               # hx-confirm's dialog
    # The mock's list doesn't change, so the same row comes back; mark this one so no twin does, as live.
    p.c.js("document.querySelector('#delegates button[hx-post] .sr-only').textContent += ' (gone)'")
    p.click("#delegates button[hx-post]", action="focus")
    p.key("Enter")
    p.settle("/Removed helpdesk/.test(document.getElementById('delegates').innerText)")
    assert p.focused()["id"] == "delegates"
    assert p.c.js("window.__errs") == []


# Record what #live-status is told, and each time the #busy pill comes on.
LISTEN = """window.__said = []; window.__busy = 0;
const live = document.getElementById('live-status'), busy = document.getElementById('busy');
new MutationObserver(() => __said.push(live.textContent)).observe(live, {childList: true, characterData: true, subtree: true});
new MutationObserver(() => { if (busy.classList.contains('on')) __busy++; }).observe(busy, {attributes: true});"""


@pytest.mark.a11y
@pytest.mark.timeout(120)
def test_a_polled_job_speaks_through_one_stable_live_region(page):
    """Plan A3, read off Chrome's accessibility tree: an offboarding run's panel replaces itself each poll,
    but what a screen reader hears comes from base.html's one live region — the same node throughout, told
    the progress and then the result, never the feed — polls don't bring up the "Working…" pill, and every
    ✓/✗ in the log is hidden behind a word."""
    p = page

    def live_regions() -> list[dict]:
        return [n for n in p.c.cmd("Accessibility.getFullAXTree")["nodes"]
                if not n.get("ignored") and any(pr["name"] == "live" for pr in n.get("properties", []))]

    _offboard_preview(p)
    [region] = live_regions()                          # #busy is aria-hidden, so only #live-status
    p.c.js(LISTEN)
    p.click("button", "Run offboarding")
    p.wait("document.querySelector('#offboard-result [hx-get]')")      # the running panel, polling
    p.wait("!document.getElementById('busy').classList.contains('on')")
    p.c.js("window.__busy = 0")                        # the Run click's own request showed it; polls mustn't
    if p.c.js("!!document.querySelector('#offboard-result [hx-get]')"):
        # Mid-run: the panel polling itself is no live region; the one there is is the same node as before.
        assert [n["backendDOMNodeId"] for n in live_regions()] == [region["backendDOMNodeId"]]
    p.settle(OFFBOARD_DONE, timeout=60)

    said = p.c.js("__said")
    final = p.c.js("document.querySelector('#offboard-result [data-announce]').innerText")
    assert said[0] == "Offboarding: started" and said[-1] == final.strip(), said
    assert all(re.fullmatch(r"Offboarding: \d0% done", s) for s in said[1:-1]), said
    assert len(said) <= 12 and len(set(said)) == len(said), said          # each line once
    assert p.c.js("__busy") == 0

    nodes = p.c.cmd("Accessibility.getFullAXTree")["nodes"]
    by_id = {n["nodeId"]: n for n in nodes}
    [now] = [n for n in nodes if not n.get("ignored") and any(pr["name"] == "live" for pr in n.get("properties", []))]
    assert now["backendDOMNodeId"] == region["backendDOMNodeId"]         # never replaced
    props = {pr["name"]: pr["value"].get("value") for pr in now["properties"]}
    assert (now["role"]["value"], props["live"], props["atomic"]) == ("status", "polite", True)
    assert [by_id[c]["name"]["value"] for c in now["childIds"]] == [said[-1]]

    texts = [n["name"]["value"] for n in nodes if not n.get("ignored") and n["role"]["value"] == "StaticText"]
    rows = p.c.js("[...document.querySelectorAll('#offboard-result li')].map(li => li.textContent.trim()[0])")
    assert rows and not [t for t in texts if t.strip()[:1] in ("✓", "✗")]
    assert sorted(t for t in texts if t in ("Succeeded:", "Failed:")) == \
        sorted({"✓": "Succeeded:", "✗": "Failed:"}[r] for r in rows if r in "✓✗")

    # app.js speaks a panel's line once per job, however many polls repeat it.
    p.c.js("""(async () => { const el = document.createElement('div'); el.dataset.announce = 'probe';
        document.body.append(el);
        for (const t of ['Probe: 10% done', 'Probe: 10% done', 'Probe: 20% done', 'Probe: 20% done']) {
          el.textContent = t; el.dispatchEvent(new CustomEvent('htmx:afterSettle', {bubbles: true}));
          await new Promise(r => setTimeout(r, 50)); }
        el.remove(); })()""")
    assert p.c.js("__said")[len(said):] == ["Probe: 10% done", "Probe: 20% done"]


def _template_tags() -> list[tuple[str, int, str, dict[str, str | None], bool]]:
    """(template, line, tag, attrs, inside a <label>) for every start tag in every template. Jinja
    statements and comments are dropped, so an attribute a condition adds counts as present."""
    found: list[tuple[str, int, str, dict[str, str | None], bool]] = []

    class Tags(HTMLParser):
        def __init__(self, name: str):
            super().__init__()
            self.name, self.open = name, []

        def handle_starttag(self, tag, attrs):
            found.append((self.name, self.getpos()[0], tag, dict(attrs), "label" in self.open))
            if tag not in ("input", "br", "img", "meta", "link", "hr"):
                self.open.append(tag)

        def handle_endtag(self, tag):
            if tag in self.open:
                while self.open.pop() != tag:
                    pass

    for path in sorted((ROOT / "gamgui" / "web" / "templates").glob("*.html")):
        # A dropped statement keeps its newlines, so the line numbers stay the template's.
        text = re.sub(r"{%.*?%}|{#.*?#}", lambda m: "\n" * m.group().count("\n"), path.read_text(), flags=re.S)
        Tags(path.name).feed(text)
    return found


def test_every_form_field_in_a_template_has_an_accessible_name():
    """Plan A4: a wrapping <label>, a <label for>, aria-label or aria-labelledby — a placeholder or a
    title alone is no name (it vanishes on typing, and axe's label-title-only flags it). Only the states
    tests' axe walk visits are checked in Chrome; this reads every template."""
    tags = _template_tags()
    label_for = {(name, a["for"]) for name, _, tag, a, _ in tags if tag == "label" and a.get("for")}
    unnamed = [
        f"{name}:{line} <{tag} name={a.get('name')!r}>"
        for name, line, tag, a, in_label in tags
        if tag in ("input", "select", "textarea")
        and a.get("type") not in ("hidden", "submit", "button")
        and "hidden" not in a and a.get("aria-hidden") != "true"
        and not (in_label or a.get("aria-label") or a.get("aria-labelledby") or (name, a.get("id")) in label_for)
    ]
    assert not unnamed, "form fields with no accessible name:\n  " + "\n  ".join(unnamed)


def test_a_focus_target_can_take_focus_and_a_dropped_outline_leaves_a_ring():
    """Plan A5: app.js focuses a swapped-in panel's [data-focus], which does nothing unless it is a control
    or carries tabindex; and a control whose class drops the outline (focus:outline-none) must keep
    another indicator (focus:ring-*), or base.html's focus-visible ring is all it had."""
    tags = _template_tags()
    inert = [f"{name}:{line} <{tag}>" for name, line, tag, a, _ in tags
             if "data-focus" in a and tag not in ("input", "select", "textarea", "button", "a")
             and a.get("tabindex") != "-1"]
    assert not inert, "[data-focus] that focus() can't reach:\n  " + "\n  ".join(inert)
    ringless = [f"{path.name}: {cls}"
                for path in sorted((ROOT / "gamgui" / "web").rglob("*.*")) if path.suffix in (".html", ".js")
                and "vendor" not in path.parts
                for cls in re.findall(r"""["']([^"'\n]*focus:outline-none[^"'\n]*)["']""", path.read_text())
                if "focus:ring-" not in cls]
    assert not ringless, "focus:outline-none with no focus:ring-:\n  " + "\n  ".join(ringless)


def test_each_tab_strip_is_an_aria_tablist():
    """Plan A2: each tab controls a tabpanel that names it back, exactly one starts selected and in the
    Tab order, and the rest start out of it (app.js keeps it so)."""
    tags = _template_tags()
    strips = {name for name, _, _, a, _ in tags if a.get("role") == "tablist"}
    assert strips == {"user_detail.html", "onboarding.html"}
    for name in strips:
        mine = [a for n, _, _, a, _ in tags if n == name]
        tabs = [a for a in mine if a.get("role") == "tab"]
        panels = {a["id"]: a for a in mine if a.get("role") == "tabpanel"}
        assert len(tabs) == len(panels) >= 2, name
        for t in tabs:
            assert panels[t["aria-controls"]]["aria-labelledby"] == t["id"], (name, t["id"])
        assert [(t["aria-selected"], t["tabindex"]) for t in tabs] == \
            [("true", "0")] + [("false", "-1")] * (len(tabs) - 1), name


# Plan A3: each polled job panel, the context its route renders it with, what its progress says, and
# its job's kind (web/jobs.py PANELS). Every loop can end with job.error — a stop at an account-wide
# failure, or Stop (plan U5) — and its running panel carries the Stop button.
POLLED = {
    "_sig_apply.html": ("job", "Applying the signature", "signatures"),
    "_bulk_apply.html": ("job", "Setting department", "department"),
    "_calendar_subscribe_job.html": ("subscribe_job", "Adding to calendars", "subscribe"),
    "_offboard_run.html": ("job", "Offboarding", "offboard"),
    "_sequence_run.html": ("job", "Running the sequence", "sequence"),
    "_onboard_bulk_status.html": ("job", "Onboarding", "onboard"),
    "_calendar_index_job.html": ("job", None, "index"),          # a scan with no count: it says so once
}


def _job_states():
    """(state, job): a run just started; 3 of 8 done with a ✓ and a ✗ in its feed and log; finished; stopped."""
    from gamgui.web.jobs import BatchJob

    def job(done: int, finished: bool = False, error: str | None = None) -> BatchJob:
        j = BatchJob(total=8)
        j.account_created = j.notified = 0          # onboarding's tallies
        for i in range(done):
            j.record(f"user{i}@example.com", ok=i != 1, reason="" if i != 1 else "Not found.")
            j.log.append(("✓ " if i != 1 else "✗ ") + f"Step {i}" + (" — boom" if i == 1 else ""))
        j.error = error
        if finished:
            j.finish()
        return j
    return [("started", job(0)), ("running", job(3)), ("finished", job(3, finished=True)),
            ("stopped", job(3, finished=True, error="Account-wide failure."))]


def _render(template: str, **ctx) -> list[tuple[str, list[tuple[str, dict]]]]:
    """Every text run of a rendered partial: (text, its open elements as (tag, attrs), outermost first)."""
    from gamgui.web.server import TEMPLATES

    runs: list = []

    class Walk(HTMLParser):
        def __init__(self):
            super().__init__()
            self.open: list[tuple[str, dict]] = []

        def handle_starttag(self, tag, attrs):
            if tag not in ("input", "br", "img", "meta", "link", "hr"):
                self.open.append((tag, dict(attrs)))

        def handle_endtag(self, tag):
            while self.open and self.open.pop()[0] != tag:
                pass

        def handle_data(self, data):
            if data.strip():
                runs.append((data, list(self.open)))

    Walk().feed(TEMPLATES.env.get_template(template).render(**ctx))
    return runs


def test_each_polled_panel_says_one_line_and_names_its_marks():
    """Plan A3: a polled panel is never a live region itself (base.html's #live-status is, outside every
    swap); each render marks one element data-announce with the job's id — progress in 10% steps, then the
    result — and every ✓/✗ in a feed or log row is hidden from a screen reader behind a word."""
    for template, (key, label, kind) in POLLED.items():
        for state, job in _job_states():
            job.kind = kind
            runs = _render(template, **{key: job}, revoke_label="Step 1", credentials=None)
            where = f"{template} {state}"
            attrs = [a for _, stack in runs for _, a in stack]
            assert not [a for a in attrs if "aria-live" in a or a.get("role") in ("status", "log", "alert")], where
            said = {a["data-announce"] for a in attrs if "data-announce" in a}
            assert said == {job.id}, where
            text = " ".join(t for t, stack in runs if any("data-announce" in a for _, a in stack))
            text = re.sub(r"\s+", " ", text).strip()
            if label and not job.finished:
                assert text == f"{label}: " + ("started" if state == "started" else "30% done"), where
            elif job.finished:
                assert "%" not in text and ("Account-wide failure." in text) == (state == "stopped"), where
            for t, stack in runs:
                in_row = any(tag == "li" for tag, _ in stack)
                hidden = any(a.get("aria-hidden") == "true" for _, a in stack)
                if in_row and t.strip()[:1] in ("✓", "✗"):
                    assert hidden, f"{where}: a bare {t.strip()[:1]} in a row"
            words = [t.strip() for t, stack in runs if ("span", {"class": "sr-only"}) in stack]
            glyphs = [t.strip() for t, stack in runs if any(a.get("aria-hidden") == "true" for _, a in stack)]
            assert [{"✓": "Succeeded:", "✗": "Failed:"}[g] for g in glyphs] == words, where


def test_the_jobs_tray_speaks_only_a_finish_and_polls_as_a_poll():
    """Plan U5: the tray re-renders every 2s on every page, so like a job panel it holds no live region of
    its own. A start is the operator's own click (its panel says "started"); each finished row carries one
    hidden data-announce line, keyed "tray-<id>" so app.js can leave it to that job's panel when it is on
    the page. Its poll path is one isPoll recognises."""
    from gamgui.web.jobs import start_job, tray

    jobs: dict = {}
    running = start_job(jobs, 8, kind="signatures", title="Signature for the whole company — 8 users")
    done = start_job(jobs, 3, kind="offboard", title="Offboarding carol@example.com")
    done.record("Reset password", True)
    done.error = "Stopped by you — 2 not attempted."
    done.finish()
    runs = _render("_jobs_tray.html", tray=tray(jobs), oob=True)
    attrs = [a for _, stack in runs for _, a in stack]
    assert not [a for a in attrs if "aria-live" in a or a.get("role") in ("status", "log", "alert")]
    assert {a["data-announce"] for a in attrs if "data-announce" in a} == {f"tray-{done.id}"}
    said = " ".join(t for t, stack in runs if any("data-announce" in a for _, a in stack))
    assert re.sub(r"\s+", " ", said).strip() == ("Finished in the background: Offboarding carol@example.com — "
                                                 "Stopped by you — 2 not attempted.")
    assert running.id in str(attrs)                          # listed, with its Stop, but not spoken
    tray_html = (ROOT / "gamgui" / "web" / "templates" / "_jobs_tray.html").read_text()
    assert re.findall(r'hx-get="([^"]+)"', tray_html) == ["/jobs/status"]
    app_js = (ROOT / "gamgui" / "web" / "static" / "app.js").read_text()
    assert 'key.indexOf("tray-") === 0' in app_js


def test_the_live_region_is_outside_every_swap_and_polls_never_flash_working():
    """Plan A3: base.html holds the one polite, atomic status region app.js writes to; and every polled
    panel's /status path is one app.js's isPoll recognises, so a poll doesn't flash "Working…" every second.
    The #busy pill is a visual cue, hidden from a reader: as a live region it said "Working…" on every
    request, each typed search too (plan A-left2)."""
    base = (ROOT / "gamgui" / "web" / "templates" / "base.html").read_text()
    assert base.count('id="live-status"') == 1
    assert '<div id="busy" aria-hidden="true">' in base and base.count("aria-live") == 1
    assert re.search(r'<div id="live-status" class="sr-only" role="status" aria-live="polite" aria-atomic="true">'
                     r'</div>', base)
    app_js = (ROOT / "gamgui" / "web" / "static" / "app.js").read_text()
    poll = re.search(r"return (/.+/)\.test\(p\);", app_js).group(1)
    assert poll == r"/\/status(\?|$)/"
    for template in POLLED:
        paths = re.findall(r'hx-get="([^"]+)"', (ROOT / "gamgui" / "web" / "templates" / template).read_text())
        assert paths and all(re.search(r"/status(\?|$)", path) for path in paths), template


def test_a_builder_command_says_its_risk_in_words():
    """Plan A3: the catalog's coloured dot is decoration; the risk is also a word on the row."""
    for risk in ("read", "change", "destructive"):
        c = type("C", (), {"raw_syntax": "gam x", "risk_label": risk, "name": "gam x", "description": "Does x.",
                           "buildable": True, "id": "x"})
        runs = _render("_command_row.html", c=c)
        visible = [t for t, stack in runs if not any(a.get("aria-hidden") == "true" for _, a in stack)]
        assert risk.capitalize() in visible and " · Does x." in visible, risk
    row = (ROOT / "gamgui" / "web" / "templates" / "_command_row.html").read_text()
    assert re.search(r'<span aria-hidden="true" class="[^"]*rounded-full', row)


def test_the_ratchet_fails_a_new_or_grown_count_and_asks_to_lower_a_shrunk_one():
    base = {"users": {"color-contrast": 3, "label": 1}}
    assert _ratchet(base, base) == ([], [])
    worse, better = _ratchet({"users": {"color-contrast": 4}, "home": {"label": 1}}, base)
    assert worse == ["home label: 1 (baseline 0)", "users color-contrast: 4 (baseline 3)"]
    assert better == ["users label: 0 (baseline 1)"]
    assert json.loads(BASELINE.read_text()) is not None       # the committed baseline parses


def test_the_vendored_axe_matches_its_recorded_checksum():
    assert _axe_source().startswith("/*! axe v")
