// The Builder screen (builder.html): the Run button kept in reach, the User/Group type-ahead, the
// result-table filter + pager, and the act-on-an-address menu.
(function () {
  "use strict";

  // After a Preview swaps the confirm panel into the result area, scroll the build pane so its
  // Run button is in reach (the pane scrolls as one unit, so a tall form can't leave Run below the
  // fold). Only when an actionable Run/confirm is present — not for a plain results table.
  document.addEventListener("htmx:afterSwap", function (e) {
    if (e.target && e.target.id === "builder-result"
        && e.target.querySelector('[hx-post="/builder/run"]')) {
      // Instant, not smooth — WKWebView ignores programmatic smooth scrolling on overflow panes.
      // Scroll now and again after layout settles so we reliably reach the bottom (the Run row).
      var pane = document.getElementById("build-pane");
      if (pane) {
        var toBottom = function () { pane.scrollTop = pane.scrollHeight; };
        toBottom();
        setTimeout(toBottom, 60);
      }
    }
  });

  // Server-backed type-ahead for the User/Group slot pickers. A real, visible dropdown that renders
  // in WKWebView (plain DOM, no <datalist>) and scales — /builder/pick returns only the top matches
  // from the cached directory. Document-level delegation so it works on HTMX-injected forms.
  (function () {
    var timer;
    function menuOf(inp) { var w = inp.closest(".upick-wrap"); return w ? w.querySelector(".upick-menu") : null; }
    function closeAll() { document.querySelectorAll(".upick-menu").forEach(function (m) { m.classList.add("hidden"); }); }
    function search(inp) {
      var menu = menuOf(inp);
      if (!menu) return;
      var kind = inp.dataset.kind || "users";
      fetch("/builder/pick?kind=" + encodeURIComponent(kind) + "&q=" + encodeURIComponent(inp.value.trim()))
        .then(function (r) { return r.text(); })
        .then(function (html) { menu.innerHTML = html; menu.classList.remove("hidden"); })
        .catch(function () {});
    }
    document.addEventListener("input", function (e) {
      if (!e.target.classList || !e.target.classList.contains("upick")) return;
      clearTimeout(timer);
      timer = setTimeout(function () { search(e.target); }, 180);
    });
    document.addEventListener("focusin", function (e) {
      if (e.target.classList && e.target.classList.contains("upick")) search(e.target);
    });
    // mousedown (not click) so the pick registers before the input's blur hides the menu.
    document.addEventListener("mousedown", function (e) {
      var opt = e.target.closest ? e.target.closest(".upick-opt") : null;
      if (opt) {
        e.preventDefault();
        var inp = opt.closest(".upick-wrap").querySelector(".upick");
        inp.value = opt.dataset.val;
        closeAll();
        return;
      }
      var onInput = e.target.classList && e.target.classList.contains("upick");
      var inMenu = e.target.closest && e.target.closest(".upick-menu");
      if (!onInput && !inMenu) closeAll();
    });
  })();

  // Result tables: grep/awk-in-the-GUI. A live row filter + an "External only" toggle (anything
  // outside your own domain — the sharing-audit lens) drive a 10-row pager over the MATCHING rows.
  (function () {
    var root = document.getElementById("builder-root");
    var DOMAIN = ((root && root.dataset.domain) || "").toLowerCase();
    var EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;
    function btn(label) {
      var b = document.createElement("button");
      b.type = "button"; b.textContent = label;
      b.className = "rounded border border-brand-gray/40 px-2 py-1 text-brand-blueink hover:bg-paper disabled:opacity-30";
      return b;
    }
    function isExternal(text) {
      var t = text.toLowerCase();
      if (t.includes("anyone")) return true;           // public / anyone-with-link
      var emails = t.match(EMAIL);
      if (!emails) return false;
      return DOMAIN ? emails.some(function (e) { return !e.endsWith("@" + DOMAIN); }) : false;
    }
    function setup(scope) {
      (scope || document).querySelectorAll("table.js-paged:not([data-rt])").forEach(function (tbl) {
        tbl.dataset.rt = "1";
        var per = 10, cur = 1, rows = Array.prototype.slice.call(tbl.tBodies[0].rows);
        var tools = tbl.closest("div.rounded-2xl").querySelector(".rt-tools");
        var filterEl = tools && tools.querySelector(".rt-filter");
        var extEl = tools && tools.querySelector(".rt-external");
        var countEl = tools && tools.querySelector(".rt-count");
        var prev = btn("‹ Prev"), next = btn("Next ›"), lbl = document.createElement("span");
        lbl.className = "text-brand-grayink";
        var foot = document.createElement("div");
        foot.className = "mt-2 flex items-center justify-between border-t border-brand-gray/15 pt-2 text-xs";
        foot.appendChild(prev); foot.appendChild(lbl); foot.appendChild(next);
        tbl.parentNode.appendChild(foot);
        function matched() {
          var q = (filterEl && filterEl.value || "").toLowerCase().trim();
          var ext = extEl && extEl.checked;
          return rows.filter(function (r) {
            var txt = r.textContent;
            if (q && !txt.toLowerCase().includes(q)) return false;
            if (ext && !isExternal(txt)) return false;
            return true;
          });
        }
        function pages() { return Math.max(1, Math.ceil(matched().length / per)); }
        function render() {
          var m = matched(), total = Math.max(1, Math.ceil(m.length / per));
          if (cur > total) cur = total;
          rows.forEach(function (r) { r.style.display = "none"; });
          m.slice((cur - 1) * per, cur * per).forEach(function (r) { r.style.display = ""; });
          if (countEl) {
            var plural = rows.length === 1 ? "" : "s";
            countEl.textContent = m.length === rows.length
              ? (rows.length + " row" + plural)
              : (m.length + " of " + rows.length);
          }
          lbl.textContent = "Page " + cur + " of " + total;
          lbl.style.visibility = total > 1 ? "" : "hidden";
          prev.disabled = cur <= 1; next.disabled = cur >= total;
          foot.style.display = total > 1 ? "" : "none";
        }
        prev.addEventListener("click", function () { if (cur > 1) { cur--; render(); } });
        next.addEventListener("click", function () { if (cur < pages()) { cur++; render(); } });
        if (filterEl) filterEl.addEventListener("input", function () { cur = 1; render(); });
        if (extEl) extEl.addEventListener("change", function () { cur = 1; render(); });
        render();
      });
    }
    document.addEventListener("htmx:afterSwap", function (e) { setup(e.target); });
    setup(document);
  })();

  // Act on a person clicked in a result table: open the chosen command pre-filled with that email.
  (function () {
    var menu = document.getElementById("row-actions");
    if (!menu) return;
    var current = "";
    function hide() { menu.classList.add("hidden"); }
    document.addEventListener("click", function (e) {
      var cell = e.target.closest ? e.target.closest(".cell-act") : null;
      if (cell) {
        current = cell.dataset.email;
        document.getElementById("row-actions-email").textContent = current;
        var r = cell.getBoundingClientRect();
        menu.style.left = Math.min(r.left, window.innerWidth - 220) + "px";
        menu.style.top = Math.min(r.bottom + 4, window.innerHeight - 320) + "px";
        menu.classList.remove("hidden");
        e.preventDefault(); e.stopPropagation();
        return;
      }
      var item = e.target.closest ? e.target.closest(".ra-item") : null;
      if (item && current) {
        var param = item.dataset.param || "email";
        htmx.ajax("GET", "/builder/command/" + item.dataset.cid + "?" + param + "=" + encodeURIComponent(current),
                  { target: "#cmd-form", swap: "innerHTML" });
        hide();
        var f = document.getElementById("cmd-form");
        if (f) f.scrollIntoView({ behavior: "smooth", block: "nearest" });
        return;
      }
      if (!e.target.closest("#row-actions")) hide();
    });
  })();
})();
