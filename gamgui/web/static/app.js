// GamGUI's shared page behaviour. Every script the app runs is a same-origin file like this one, so
// the Content-Security-Policy can say `script-src 'self'` (plan Q13): no template carries an inline
// <script> or an on*= handler, and an injected one would be inert. A control says what it does with
// data-action (its arguments in other data-* attributes); one delegated listener serves the page and
// every partial htmx swaps in. A page's own script adds its actions to GamGUI.actions.
(function () {
  "use strict";

  // Insert a hint chip's snippet into its Builder slot's text field (space-separated, cursor at end).
  function insertHint(btn) {
    var slot = btn.closest("[data-slot]");
    var inp = slot && slot.querySelector("input[name]");
    if (!inp) return;
    var snip = btn.dataset.ins || "";
    var cur = (inp.value || "").replace(/\s+$/, "");
    inp.value = cur ? cur + " " + snip : snip;
    inp.focus();
    var n = inp.value.length;
    try { inp.setSelectionRange(n, n); } catch (e) { /* not a text input that supports selection */ }
  }

  // Copy the text of the code/input/textarea inside the clicked button's .copy-wrap.
  function copyText(btn) {
    var wrap = btn.closest(".copy-wrap") || btn.parentElement;
    var src = wrap && wrap.querySelector("code, pre, input, textarea");
    if (!src) return;
    var text = (src.tagName === "INPUT" || src.tagName === "TEXTAREA") ? src.value : src.innerText;
    // A button whose name carries more than it shows (a catalog row's hidden command) marks the
    // visible word [data-label]; only that word flips, so the hidden part isn't made visible.
    var label = btn.querySelector("[data-label]") || btn;
    function done() {
      var prev = label.textContent;
      label.textContent = "Copied";
      setTimeout(function () { label.textContent = prev; }, 1200);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { legacyCopy(text); done(); });
    } else {
      legacyCopy(text);
      done();
    }
  }
  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* execCommand unsupported; the async clipboard path already handled it */ }
    ta.remove();
  }

  // Print a one-time credentials sheet (_sheet_buttons.html) on its own. A browser prints a clean copy
  // from a hidden iframe; WKWebView can't print an iframe (pywebview replaces only the top-level
  // window.print), so the native window prints this page with base.html's print CSS showing only it.
  // The iframe's <style> is allowed by the page policy's style-src 'unsafe-inline', which it inherits.
  function printSheet(btn) {
    var sheet = document.getElementById(btn.dataset.sheet);
    if (!sheet) return;
    if (window.pywebview) {
      document.querySelectorAll(".print-target").forEach(function (el) { el.classList.remove("print-target"); });
      sheet.classList.add("print-target");
      document.body.classList.add("print-one");
      window.print();
      return;
    }
    var f = document.createElement("iframe");
    f.setAttribute("aria-hidden", "true");
    f.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0";
    document.body.appendChild(f);
    var d = f.contentWindow.document;
    d.open();
    d.write('<html><head><title>New account credentials</title><style>' +
      'body{font:13px -apple-system,system-ui,sans-serif;padding:32px;color:#111}h2{font-size:16px}' +
      'dl{display:grid;grid-template-columns:8rem 1fr;row-gap:8px;column-gap:16px}' +
      'dt{color:#555}dd{margin:0;font-family:ui-monospace,monospace}p{color:#555;margin-top:20px}' +
      '.sheet-cards{display:grid;grid-template-columns:1fr 1fr;gap:12px}' +
      '.sheet-cards>div{border:1px solid #ccc;border-radius:8px;padding:10px;line-height:1.6;break-inside:avoid}' +
      '.sheet-cards>div>div{font-family:ui-monospace,monospace}' +
      '.sheet-cards>div>div:first-child{font-family:-apple-system,system-ui,sans-serif;font-weight:600}' +
      '.sheet-cards>div>div:nth-child(3){font-size:15px;font-weight:700}' +
      '.sheet-cards>div>div:last-child{color:#555;font-size:11px}</style></head>' +
      '<body><h2>New account credentials</h2>' + sheet.outerHTML + '</body></html>');
    d.close();
    f.contentWindow.focus();
    f.contentWindow.print();
    setTimeout(function () { f.remove(); }, 1500);
  }

  // Focus (plan A5). A confirm/preview panel or an error that an operator's click or submit swaps in
  // takes focus — its [data-focus] heading (tabindex=-1) or control — so a keyboard or screen-reader
  // user lands in it; the control that opened it is remembered per zone. When the panel closes, focus
  // goes somewhere sensible instead of falling to <body>: a Cancel ([data-cancel], or the "clear"
  // action) returns it to that opener, or to the opener's re-rendered twin (the same kind of control
  // with the same text); a Confirm or Run that replaced the panel puts it on the zone, so its result is
  // read. A swap from a load, a poll, typing or a changed select never moves focus.
  var openers = new WeakMap();
  function byOperator(detail) {
    var t = detail.requestConfig && detail.requestConfig.triggeringEvent;
    return !!t && (t.type === "click" || t.type === "submit");
  }
  function focusZone(zone) {
    if (!zone.isConnected) return;
    if (!zone.hasAttribute("tabindex")) zone.setAttribute("tabindex", "-1");
    zone.focus();
  }
  function returnFocus(zone, opener) {
    if (opener.isConnected) { opener.focus(); return true; }
    var text = opener.textContent.trim();
    var twin = [].find.call(zone.querySelectorAll(opener.tagName), function (el) {
      return el.textContent.trim() === text;
    });
    if (twin) { twin.focus(); return true; }
    return false;
  }
  // A click or submit whose swap replaced the very control it came from (the title/department Save, a
  // delegate's Remove) drops focus to <body>. Once every root of that swap has settled (htmx settles the
  // out-of-band ones first), focus goes to the control's re-rendered twin — the Save — or, when it is
  // gone, to the zone, which opens with the result. One check per request; focus that survived stays.
  var settled = new WeakMap();
  function regainLater(d, root) {
    var roots = settled.get(d);
    if (roots) { roots.push(root); return; }
    settled.set(d, roots = [root]);
    setTimeout(function () {
      var active = document.activeElement;
      if (active && active !== document.body && active.isConnected) return;
      var src = d.requestConfig.triggeringEvent.submitter || d.requestConfig.elt;
      var homes = d.target.isConnected ? [d.target] : roots.filter(function (r) { return r.isConnected; });
      if (!homes.length || homes.some(function (h) { return returnFocus(h, src); })) return;
      focusZone(homes[homes.length - 1]);
    }, 0);
  }
  document.body.addEventListener("htmx:afterSettle", function (e) {
    var d = e.detail, root = e.target;
    if (!byOperator(d) || !root.querySelector) return;
    var zone = d.target, cfg = d.requestConfig;
    var panel = root.matches("[data-focus]") ? root : root.querySelector("[data-focus]");
    if (panel) {
      // A control outside the zone opened it; one inside (the panel re-rendered with an error, or an
      // in-zone button the panel replaced) keeps the opener already known, if any.
      var src = cfg.triggeringEvent.submitter || cfg.elt;
      if (src.isConnected || !openers.has(zone)) openers.set(zone, src);
      panel.focus();
      return;
    }
    var opener = openers.get(zone);
    if (!opener) { regainLater(d, root); return; }
    openers.delete(zone);
    var active = document.activeElement;
    if (active && active !== document.body && active.isConnected) return;   // focus survived the swap
    if (!(cfg.elt.hasAttribute("data-cancel") && returnFocus(zone, opener))) focusZone(zone);
  });

  // Polled progress (plan A3). A job panel replaces itself every second, so it can't be a live region:
  // a screen reader would hear nothing, or the whole feed again. Each render marks one element
  // data-announce="<job id>" (templates/_job_live.html) — progress in 10% steps, then the result — and
  // its text goes to base.html's #live-status, which no swap replaces, only when it changed for that job.
  // The jobs tray (plan U5) marks a finished job "tray-<job id>": said only when that job's own panel
  // isn't on the page, so its result is never talked over.
  var said = {};
  // What the page already showed on load isn't news: only a change after it is spoken (the jobs tray's
  // finished rows are in every page, and its next poll repeats them).
  [].forEach.call(document.querySelectorAll("[data-announce]"), function (el) {
    said[el.dataset.announce] = el.innerText.replace(/\s+/g, " ").trim();
  });
  document.body.addEventListener("htmx:afterSettle", function (e) {
    var root = e.target, live = document.getElementById("live-status");
    if (!live || !root.querySelectorAll) return;
    // A line the page no longer shows is forgotten: said again when it comes back ("Added …" after a
    // Remove in between) is news again.
    Object.keys(said).forEach(function (key) {
      if (!document.querySelector('[data-announce="' + CSS.escape(key) + '"]')) delete said[key];
    });
    var marked = [].slice.call(root.querySelectorAll("[data-announce]"));
    if (root.matches("[data-announce]")) marked.unshift(root);
    marked.forEach(function (el) {
      var key = el.dataset.announce;
      var text = el.innerText.replace(/\s+/g, " ").trim();   // innerText: a <br> is a break, not nothing
      if (!text || said[key] === text) return;
      said[key] = text;
      if (key.indexOf("tray-") === 0 && document.querySelector('[data-announce="' + key.slice(5) + '"]')) return;
      live.textContent = text;
    });
  });

  // The header's jobs tray (plan U5, base.html): a disclosure button and the list it shows. Escape, a click
  // outside, or focus moving on out of it closes it: an open tray left behind covers the controls focus
  // goes to next (WCAG 2.4.11). Escape hands focus back to the button when it was in the tray, or lost —
  // never out of a field the operator is in.
  function setTray(open, refocus) {
    var btn = document.getElementById("jobs-toggle"), panel = document.getElementById("jobs-panel");
    if (!btn || !panel) return;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    panel.hidden = !open;
    if (!open && refocus) btn.focus();
  }
  function trayOpen() {
    var panel = document.getElementById("jobs-panel");
    return !!panel && !panel.hidden;
  }
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape" || !trayOpen()) return;
    var tray = document.getElementById("jobs-tray"), at = document.activeElement;
    setTray(false, !at || at === document.body || tray.contains(at));
  });
  document.addEventListener("focusout", function (e) {
    var tray = document.getElementById("jobs-tray");
    // No relatedTarget is focus lost, not moved: a poll swapping out the focused row, or the window
    // losing focus. Only a move to somewhere outside the tray closes it.
    if (trayOpen() && tray && tray.contains(e.target) && e.relatedTarget && !tray.contains(e.relatedTarget)) {
      setTray(false, false);
    }
  });
  document.addEventListener("click", function (e) {
    var tray = document.getElementById("jobs-tray");
    // A target a poll just swapped out is no longer in the tray, but was clicked inside it.
    if (trayOpen() && e.target.isConnected && !tray.contains(e.target)) setTray(false, false);
  });

  var actions = {
    "copy": copyText,
    "jobs-toggle": function (btn) { setTray(btn.getAttribute("aria-expanded") !== "true", false); },
    "hint": insertHint,
    "print-sheet": printSheet,
    // A confirm step's Cancel: empty the zone it was swapped into, and hand focus back to its opener.
    "clear": function (btn) {
      var zone = document.getElementById(btn.dataset.clear);
      if (!zone) return;
      zone.replaceChildren();
      var opener = openers.get(zone);
      openers.delete(zone);
      if (opener) returnFocus(zone, opener);
    },
    // Bring a region into view once the request this control also fired has had time to swap it.
    "scroll-to": function (btn) {
      setTimeout(function () {
        var el = document.getElementById(btn.dataset.scrollTo);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }, 200);
    },
  };
  window.GamGUI = { actions: actions };

  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-action]");
    var run = el && actions[el.dataset.action];
    if (run) run(el, e);
  });

  // A typed confirmation (signature Apply over the threshold): its button unlocks on the exact value.
  document.addEventListener("input", function (e) {
    var inp = e.target;
    if (!inp.dataset || !inp.dataset.unlocks) return;
    var btn = document.getElementById(inp.dataset.unlocks);
    if (btn) btn.disabled = inp.value.trim() !== inp.dataset.expect;
  });

  // Tabs (user detail, onboarding): show one panel at a time so a content-heavy page fits the window.
  // ARIA tabs (plan A2): only the selected tab is in the Tab order (roving tabindex); Left/Right (wrapping),
  // Home and End move to a tab and show its panel, as a click does.
  // A panel's [data-lazy] regions (hx-trigger="tab-shown") load the first time their tab shows, not all
  // with the page (plan U13). A strip marked data-hash keeps its tab in the URL's #hash — replaced, not a
  // new history entry — so Back or a reload reopens the same tab (plan U8).
  function whenBound(fn) {   // htmx binds the page on DOMContentLoaded, before this listener runs
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
    else fn();
  }
  document.querySelectorAll("[role=tablist]").forEach(function (bar) {
    var tabs = [].slice.call(bar.querySelectorAll("[role=tab]"));
    var name = function (t) { return t.id.replace(/^tab-/, ""); };
    function show(tab, remember) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        t.classList.toggle("text-brand-black", on);
        t.classList.toggle("border-brand-blue", on);
        t.classList.toggle("text-brand-grayink", !on);
        t.classList.toggle("border-transparent", !on);
        var panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) panel.classList.toggle("hidden", !on);
      });
      var shown = document.getElementById(tab.getAttribute("aria-controls"));
      var lazy = shown ? [].slice.call(shown.querySelectorAll("[data-lazy]")) : [];
      lazy.forEach(function (el) { el.removeAttribute("data-lazy"); });
      if (lazy.length) whenBound(function () { lazy.forEach(function (el) { htmx.trigger(el, "tab-shown"); }); });
      if (remember && bar.hasAttribute("data-hash")) history.replaceState(history.state, "", "#" + name(tab));
    }
    bar.addEventListener("click", function (e) {
      var t = e.target.closest("[role=tab]");
      if (t) show(t, true);
    });
    bar.addEventListener("keydown", function (e) {
      var i = tabs.indexOf(e.target);
      var to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
      if (i < 0 || to === undefined || e.altKey || e.ctrlKey || e.metaKey) return;   // Cmd+Left is Back
      e.preventDefault();
      var t = tabs[(to + tabs.length) % tabs.length];
      show(t, true);
      t.focus();
    });
    var hashed = bar.hasAttribute("data-hash") && tabs.filter(function (t) { return "#" + name(t) === location.hash; })[0];
    if (tabs.length) show(hashed || bar.querySelector("[aria-selected=true]") || tabs[0], false);
  });

  // Global activity indicators: a top bar + a "Working…" pill, shown during HTMX requests and
  // full-page navigations so it's never ambiguous whether the app is busy.
  var bar = document.getElementById("loadbar");
  var pill = document.getElementById("busy");
  var pending = 0, hideTimer = null;
  function show() {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    if (bar) bar.classList.add("on");
    if (pill) pill.classList.add("on");
  }
  function hide() {
    if (hideTimer) clearTimeout(hideTimer);
    // Brief debounce so back-to-back requests don't make the indicators blink.
    hideTimer = setTimeout(function () {
      if (bar) bar.classList.remove("on");
      if (pill) pill.classList.remove("on");
      hideTimer = null;
    }, 180);
  }
  // A job's progress panel polls its /status every second and shows its own spinner and count — don't
  // let those background polls flicker the global pill (base.html hides it from a reader: a cue, not news).
  function isPoll(evt) {
    try {
      var d = evt.detail || {};
      var p = (d.pathInfo && d.pathInfo.requestPath) || (d.requestConfig && d.requestConfig.path) || "";
      return /\/status(\?|$)/.test(p);
    } catch (e) { return false; }
  }
  function settle(e) {
    if (isPoll(e)) return;
    pending = Math.max(0, pending - 1);
    if (pending === 0) hide();
  }
  document.body.addEventListener("htmx:beforeRequest", function (e) { if (isPoll(e)) { return; } pending++; show(); });
  document.body.addEventListener("htmx:afterRequest", settle);
  document.body.addEventListener("htmx:responseError", settle);
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a) return;
    var href = a.getAttribute("href") || "";
    if (a.target === "_blank" || href.charAt(0) === "#" || href.includes("://")) return;
    show();  // full-page nav — cleared automatically when the new page loads
  }, true);
})();
