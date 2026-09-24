// The Groups board (groups.html and its partials). The group finder marks the picked group; the
// add-member field is an ARIA combobox: typing asks /groups/people for the top matches, ArrowDown/Up
// move through them, Enter takes one (with none picked it submits the typed address), Escape closes
// the list. After a successful add the field is emptied and refocused for the next person. Addresses
// are read off autoescaped data-* attributes, never generated into JS: they are directory data.
(function () {
  "use strict";

  // The group finder: mark the picked group, and remember it so a new search keeps it marked.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-group]");
    if (!btn) return;
    document.querySelectorAll("#group-results [data-group]").forEach(function (b) {
      if (b === btn) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
    });
    var picked = document.getElementById("group-selected");
    if (picked) picked.value = btn.dataset.group;
  });

  var timer, seq = 0;
  function field() { return document.getElementById("member-email"); }
  function popup() { return document.getElementById("people-results"); }
  function options() {
    var p = popup();
    return p && !p.classList.contains("hidden") ? [].slice.call(p.querySelectorAll("[role=option]")) : [];
  }
  function say(text) {
    var live = document.getElementById("live-status");
    if (live) live.textContent = text;
  }
  function close() {
    var inp = field(), p = popup();
    seq++;                                   // a search still in flight must not reopen it
    clearTimeout(timer);
    if (p) { p.classList.add("hidden"); p.replaceChildren(); }
    if (inp) { inp.setAttribute("aria-expanded", "false"); inp.removeAttribute("aria-activedescendant"); }
  }
  function search(inp) {
    var q = inp.value.trim(), n = ++seq;
    if (!q) { close(); return; }
    fetch("/groups/people?q=" + encodeURIComponent(q), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : ""; })
      .then(function (html) {
        if (n !== seq || document.activeElement !== inp) return;   // a newer search, or the field was left
        var p = popup();
        p.innerHTML = html;
        p.classList.toggle("hidden", !html.trim());
        var count = options().length;
        inp.setAttribute("aria-expanded", count ? "true" : "false");
        inp.removeAttribute("aria-activedescendant");
        say(count ? count + (count === 1 ? " suggestion" : " suggestions") + ", arrow down to pick"
          : "No suggestions; Add uses the address as typed");
      })
      .catch(function () { /* no suggestions; the typed address still works */ });
  }
  function activate(opt) {
    options().forEach(function (o) { o.setAttribute("aria-selected", o === opt ? "true" : "false"); });
    field().setAttribute("aria-activedescendant", opt.id);
    opt.scrollIntoView({ block: "nearest" });
  }
  function take(opt) {
    var inp = field();
    inp.value = opt.dataset.val;
    close();
    inp.focus();
  }

  document.addEventListener("input", function (e) {
    if (e.target.id !== "member-email") return;
    var inp = e.target;
    clearTimeout(timer);
    timer = setTimeout(function () { search(inp); }, 200);
  });
  document.addEventListener("keydown", function (e) {
    if (e.target.id !== "member-email" || e.altKey || e.ctrlKey || e.metaKey) return;
    var opts = options();
    var cur = opts.findIndex(function (o) { return o.getAttribute("aria-selected") === "true"; });
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && opts.length) {
      e.preventDefault();
      var down = e.key === "ArrowDown";
      activate(opts[cur < 0 ? (down ? 0 : opts.length - 1) : (cur + (down ? 1 : -1) + opts.length) % opts.length]);
    } else if (e.key === "Enter" && cur >= 0) {
      e.preventDefault();                    // take the suggestion; don't submit yet
      take(opts[cur]);
    } else if (e.key === "Escape" && opts.length) {
      e.preventDefault();
      close();
    }
  });
  // mousedown, not click: the pick lands before the field's blur closes the list.
  document.addEventListener("mousedown", function (e) {
    var opt = e.target.closest && e.target.closest("#people-results [role=option]");
    if (!opt) return;
    e.preventDefault();
    take(opt);
  });
  document.addEventListener("focusout", function (e) { if (e.target.id === "member-email") close(); });
  document.addEventListener("submit", function (e) { if (e.target.id === "member-add") close(); }, true);

  // A successful add (its message carries data-added): empty the field for the next person. Its message
  // is spoken through #live-status (data-announce), so focus can stay in the form.
  document.body.addEventListener("htmx:afterSettle", function (e) {
    if (e.target.id !== "member-list" || !e.target.querySelector("[data-added]")) return;
    var inp = field();
    if (inp) { inp.value = ""; inp.focus(); }
  });
})();
