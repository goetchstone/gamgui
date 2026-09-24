// GamGUI's shared page behaviour. Every script the app runs is a same-origin file like this one, so
// the Content-Security-Policy can say `script-src 'self'` (plan Q13): no template carries an inline
// <script> or an on*= handler, and an injected one would be inert. A control says what it does with
// data-action (its arguments in other data-* attributes); one delegated listener serves the page and
// every partial htmx swaps in. A page's own script adds its actions to GamGUI.actions.
(function () {
  "use strict";

  // Insert a hint chip's snippet into the text field in the same label (space-separated, cursor at end).
  function insertHint(btn) {
    var label = btn.closest("label");
    var inp = label && label.querySelector("input[name]");
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
    function done() {
      var prev = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(function () { btn.textContent = prev; }, 1200);
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

  var actions = {
    "copy": copyText,
    "hint": insertHint,
    "print-sheet": printSheet,
    // A confirm step's Cancel: empty the zone it was swapped into.
    "clear": function (btn) {
      var zone = document.getElementById(btn.dataset.clear);
      if (zone) zone.replaceChildren();
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
  document.querySelectorAll("[data-tabs]").forEach(function (bar) {
    var tabs = bar.querySelectorAll("[data-tab]");
    var panels = document.querySelectorAll("[data-panel]");
    function show(name) {
      panels.forEach(function (p) { p.classList.toggle("hidden", p.dataset.panel !== name); });
      tabs.forEach(function (t) {
        var on = t.dataset.tab === name;
        t.classList.toggle("text-brand-black", on);
        t.classList.toggle("border-brand-blue", on);
        t.classList.toggle("text-brand-gray", !on);
        t.classList.toggle("border-transparent", !on);
      });
    }
    tabs.forEach(function (t) { t.addEventListener("click", function () { show(t.dataset.tab); }); });
    if (tabs.length) show(tabs[0].dataset.tab);
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
  // The signature-apply progress panel polls itself every second and shows its own spinner +
  // live count — don't let those background polls flicker the global pill.
  function isPoll(evt) {
    try {
      var d = evt.detail || {};
      var p = (d.pathInfo && d.pathInfo.requestPath) || (d.requestConfig && d.requestConfig.path) || "";
      return p.includes("/apply/status");
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
