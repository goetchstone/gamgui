// The signature designer (signatures.html): the "Which" list for the chosen scope, and loading a
// saved template into the editor.
(function () {
  "use strict";

  var type = document.getElementById("scope_type");
  var which = document.getElementById("scope_value");

  // Each scope's choices arrive as server-rendered <option>s in a <template data-scope=…>: OU paths,
  // departments, locations and names come from Google and may contain <, > or ", so they are escaped
  // by Jinja as element text and never pass through a JS string or innerHTML. The test user's list
  // opens on the connected admin (the template marks it selected) or on "Choose a user…" — never on
  // whoever sorts first, a colleague whose signature Apply would overwrite.
  function populate() {
    var tpl = document.querySelector('template[data-scope="' + type.value + '"]');
    which.replaceChildren();
    if (tpl) which.appendChild(tpl.content.cloneNode(true));
    var any = which.options.length > 0;
    which.disabled = !any;
    which.style.visibility = any ? "visible" : "hidden";
  }
  if (type && which) {
    type.addEventListener("change", populate);
    populate();
  }

  // Load a saved template into the editor WITHOUT a round trip: the HTML is carried in the button's
  // data-body (Jinja attribute-escaped, so it's inert text here — never innerHTML'd), dropped straight
  // into the textarea value, then the existing Preview button is clicked to refresh the preview.
  GamGUI.actions["sig-load"] = function (btn) {
    var ta = document.querySelector("#sig-form textarea[name=template]");
    if (!ta) return;
    ta.value = btn.dataset.body || "";
    ta.focus();
    var preview = document.getElementById("sig-preview-btn");
    if (preview) preview.click();
  };
})();
