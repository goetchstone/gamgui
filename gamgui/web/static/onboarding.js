// The onboarding screen (onboarding.html): the account-creation fields, editing a role template,
// and adding a picked group or calendar to it.
(function () {
  "use strict";

  function roleForm() { return document.querySelector('form[hx-post="/onboard/role"]'); }

  // Reveal the first/last name fields only when "Create the Google account" is ticked.
  document.addEventListener("change", function (e) {
    var cb = e.target;
    if (!cb.dataset || !cb.dataset.reveals) return;
    var box = document.getElementById(cb.dataset.reveals);
    if (!box) return;
    // Toggle `grid` (not a static `sm:grid`, which a responsive utility would let win over `hidden`
    // at desktop widths — the fields then never hid). Only one of hidden/grid is present at a time.
    box.classList.toggle("hidden", !cb.checked);
    box.classList.toggle("grid", cb.checked);
  });

  // Edit a role: copy its name + steps + org unit + signature + groups + calendars into the form.
  GamGUI.actions["ob-edit"] = function (btn) {
    var f = roleForm();
    if (!f) return;
    f.querySelector("[name=name]").value = btn.dataset.name;
    f.querySelector("[name=steps]").value = btn.dataset.steps;
    f.querySelector("[name=org_unit]").value = btn.dataset.org || "";
    ["signature", "groups", "calendars"].forEach(function (k) {
      var el = f.querySelector("[name=" + k + "]");
      if (el) el.value = btn.dataset[k] || "";
    });
    f.querySelector("[name=name]").focus();
  };

  // Add a picked group/calendar to its textarea (deduped, one per line).
  GamGUI.actions["ob-add"] = function (btn) {
    var f = roleForm();
    var ta = f && f.querySelector('[name="' + btn.dataset.field + '"]');
    var value = btn.dataset.value;
    if (!ta || !value) return;
    var lines = ta.value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
    if (lines.indexOf(value) === -1) lines.push(value);
    ta.value = lines.join("\n") + "\n";
  };
})();
