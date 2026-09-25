// The setup wizard (setup.html). Typing the super-admin email fills in the primary domain from the
// part after "@", until the operator edits the domain field themselves (clearing it hands it back).
(function () {
  "use strict";
  var admin = document.getElementById("setup-admin");
  var domain = document.getElementById("setup-domain");
  if (!admin || !domain) return;

  domain.addEventListener("input", function () {
    domain.dataset.edited = domain.value.trim() ? "1" : "";
  });
  admin.addEventListener("input", function () {
    if (domain.dataset.edited) return;
    var at = admin.value.lastIndexOf("@");
    domain.value = at < 0 ? "" : admin.value.slice(at + 1).trim().toLowerCase();
  });
})();
