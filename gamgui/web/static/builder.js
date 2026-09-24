// The Builder screen (builder.html): the Run button kept in reach, the User/Group type-ahead and the
// act-on-an-address menu. A result's filter and pager are server-side (_records_table.html).
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
