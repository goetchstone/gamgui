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
  // from the cached directory. Each field is an ARIA combobox (_builder_form.html): ArrowDown/Up move
  // through the suggestions (ArrowDown opens them), Enter takes one without submitting, Escape closes,
  // a click still picks. Document-level delegation so it works on HTMX-injected forms.
  (function () {
    var timer, seq = 0;
    function isPick(el) { return !!(el && el.classList && el.classList.contains("upick")); }
    function menuOf(inp) { var w = inp.closest(".upick-wrap"); return w ? w.querySelector(".upick-menu") : null; }
    function options(inp) {
      var menu = menuOf(inp);
      return menu && !menu.classList.contains("hidden") ? [].slice.call(menu.querySelectorAll("[role=option]")) : [];
    }
    function close(inp) {
      var menu = menuOf(inp);
      seq++;                                   // a search still in flight must not reopen it
      clearTimeout(timer);
      if (menu) { menu.classList.add("hidden"); menu.replaceChildren(); }
      inp.setAttribute("aria-expanded", "false");
      inp.removeAttribute("aria-activedescendant");
    }
    function search(inp, speak) {
      var menu = menuOf(inp), n = ++seq;
      if (!menu) return;
      var kind = inp.dataset.kind || "users";
      fetch("/builder/pick?kind=" + encodeURIComponent(kind) + "&q=" + encodeURIComponent(inp.value.trim()))
        .then(function (r) { return r.ok ? r.text() : ""; })
        .then(function (html) {
          if (n !== seq || document.activeElement !== inp) return;   // a newer search, or the field was left
          menu.innerHTML = html;
          menu.classList.toggle("hidden", !html.trim());
          var list = menu.querySelector("[role=listbox]"), opts = options(inp);
          if (list) {
            list.id = inp.getAttribute("aria-controls");
            opts.forEach(function (o, i) { o.id = list.id + "-" + i; });
          }
          inp.setAttribute("aria-expanded", opts.length ? "true" : "false");
          inp.removeAttribute("aria-activedescendant");
          var live = document.getElementById("live-status");
          if (speak && live) {
            live.textContent = opts.length ? opts.length + (opts.length === 1 ? " suggestion" : " suggestions")
              + ", arrow down to pick" : "No matches";
          }
        })
        .catch(function () { /* no suggestions; the typed address still works */ });
    }
    function activate(inp, opt) {
      options(inp).forEach(function (o) { o.setAttribute("aria-selected", o === opt ? "true" : "false"); });
      inp.setAttribute("aria-activedescendant", opt.id);
      opt.scrollIntoView({ block: "nearest" });
    }
    function take(inp, opt) {
      inp.value = opt.dataset.val;
      close(inp);
      inp.focus();
    }
    document.addEventListener("input", function (e) {
      if (!isPick(e.target)) return;
      var inp = e.target;
      clearTimeout(timer);
      timer = setTimeout(function () { search(inp, true); }, 180);
    });
    document.addEventListener("focusin", function (e) { if (isPick(e.target)) search(e.target, false); });
    document.addEventListener("focusout", function (e) { if (isPick(e.target)) close(e.target); });
    document.addEventListener("keydown", function (e) {
      var inp = e.target;
      if (!isPick(inp) || e.ctrlKey || e.metaKey) return;
      var opts = options(inp);
      var cur = opts.findIndex(function (o) { return o.getAttribute("aria-selected") === "true"; });
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!opts.length) { if (e.key === "ArrowDown") search(inp, true); return; }
        if (e.altKey) return;                  // Alt+Down opens; with the list open, stay put
        var down = e.key === "ArrowDown";
        activate(inp, opts[cur < 0 ? (down ? 0 : opts.length - 1) : (cur + (down ? 1 : -1) + opts.length) % opts.length]);
      } else if (e.key === "Enter" && cur >= 0) {
        e.preventDefault();                    // take the suggestion; don't submit yet
        take(inp, opts[cur]);
      } else if (e.key === "Escape" && menuOf(inp) && !menuOf(inp).classList.contains("hidden")) {
        e.preventDefault();
        close(inp);
      }
    });
    // mousedown (not click) so the pick lands before the field's blur closes the list; anywhere in the
    // list keeps focus in the field.
    document.addEventListener("mousedown", function (e) {
      var menu = e.target.closest ? e.target.closest(".upick-menu") : null;
      if (!menu) return;
      e.preventDefault();
      var opt = e.target.closest("[role=option]");
      if (opt) take(menu.closest(".upick-wrap").querySelector(".upick"), opt);
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
