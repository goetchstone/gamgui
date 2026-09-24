"""Accessibility ratchet: axe-core over every main screen of the mock-backed app, in headless Chrome.

    .venv/bin/python -m pytest -m a11y -rP                          # make a11y; CI's a11y job
    A11Y_UPDATE_BASELINE=1 .venv/bin/python -m pytest -m a11y       # after a fix: lower the baseline

Drives scripts/preview_mock.py's app (strict mock gam, fake example.com data, a temp $HOME) through
each screen and the states that matter (user-detail and onboarding tabs, a signature preview, a
calendar's access, a Builder result, an offboarding preview), injects the vendored axe-core
(tests/a11y/, checksum-checked) and counts the serious/critical violations per screen and rule. It
fails on a rule or screen the baseline (tests/a11y/baseline.json) doesn't list, on a count above it —
and on a count below it, so a fix locks its gain in. The baseline only ever shrinks, to empty (it is).
A second Chrome test uses the keyboard alone (real key events): the ARIA tab strips, focus landing in a
confirm panel and coming back when it closes, and a brand focus ring on every Tab stop of every screen.

The Chrome runs are marked a11y and deselected from the default run (pyproject addopts: they start
Chrome); the checksum and ratchet-logic tests and the template checks (every field named, every focus
target focusable, every tab strip an ARIA tablist) run by default. Skipped when Google Chrome
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

    KEYS = {"Tab": 9, "Enter": 13, "End": 35, "Home": 36, "ArrowLeft": 37, "ArrowRight": 39}

    def key(self, name: str) -> None:
        """Press a key the way the keyboard does (CDP input events: Tab moves focus, Enter activates)."""
        ev = {"key": name, "code": name, "windowsVirtualKeyCode": self.KEYS[name]}
        down = {"type": "keyDown", "text": "\r"} if name == "Enter" else {"type": "rawKeyDown"}
        self.c.cmd("Input.dispatchKeyEvent", **down, **ev)
        self.c.cmd("Input.dispatchKeyEvent", type="keyUp", **ev)

    def focused(self) -> dict:
        """What has focus: its id, text, whether it is a [data-focus] panel and shows a focus indicator."""
        return self.c.js(FOCUSED) or {}

    def violations(self) -> list[dict]:
        if not self.c.js("!!window.axe"):
            self.c.js(self.axe)
        return [v for v in self.c.js(AXE_RUN, timeout=60) if v["impact"] in GATED]


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

    p.goto("/groups", "document.querySelector('#group-select')")
    p.c.js("(() => { const s = document.querySelector('#group-select'); s.selectedIndex = 1;"
           " s.dispatchEvent(new Event('change', {bubbles: true})); })()")
    p.settle("document.querySelector('#members')?.children.length")
    yield "groups"

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

    p.goto("/onboard")
    for tab in ("generate", "roles", "welcome", "bulk"):
        p.click(f"#tab-{tab}")
        p.settle()
        yield f"onboard/{tab}"

    p.goto("/lifecycle")
    p.fill("input[name=user]", "carol@example.com")
    p.fill("input[name=manager]", "alice@example.com")
    p.click("button", "Preview")
    p.settle("document.querySelector('#offboard-result code, #offboard-result pre')")
    yield "lifecycle"

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


def test_the_ratchet_fails_a_new_or_grown_count_and_asks_to_lower_a_shrunk_one():
    base = {"users": {"color-contrast": 3, "label": 1}}
    assert _ratchet(base, base) == ([], [])
    worse, better = _ratchet({"users": {"color-contrast": 4}, "home": {"label": 1}}, base)
    assert worse == ["home label: 1 (baseline 0)", "users color-contrast: 4 (baseline 3)"]
    assert better == ["users label: 0 (baseline 1)"]
    assert json.loads(BASELINE.read_text()) is not None       # the committed baseline parses


def test_the_vendored_axe_matches_its_recorded_checksum():
    assert _axe_source().startswith("/*! axe v")
