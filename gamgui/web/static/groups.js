// The Groups board (groups.html, _board_members.html): drag a person onto the group to add them, a
// member onto the people list to remove them. A card says which way it moves with data-drag (add |
// remove) and a drop zone what it accepts with data-drop. The email is read off the card's
// data-email (autoescaped by Jinja), never generated into JS: it is directory data from Google and
// may contain quotes.
(function () {
  "use strict";

  function mutate(email, op) {
    var select = document.getElementById("group-select");
    var group = select ? select.value : "";
    if (!group) return;
    htmx.ajax("POST", "/groups/members", { values: { group: group, email: email, op: op }, target: "#members", swap: "innerHTML" });
  }

  document.addEventListener("dragstart", function (e) {
    var card = e.target.closest && e.target.closest("[data-drag]");
    if (!card) return;
    e.dataTransfer.setData("text/plain", JSON.stringify({ email: card.dataset.email, op: card.dataset.drag }));
    e.dataTransfer.effectAllowed = "move";
  });
  document.addEventListener("dragover", function (e) {
    if (e.target.closest && e.target.closest("[data-drop]")) e.preventDefault();
  });
  document.addEventListener("drop", function (e) {
    var zone = e.target.closest && e.target.closest("[data-drop]");
    if (!zone) return;
    e.preventDefault();
    var p;
    try { p = JSON.parse(e.dataTransfer.getData("text/plain")); } catch (err) { return; }
    if (p && p.op === zone.dataset.drop) mutate(p.email, p.op);
  });

  var filter = document.getElementById("people-filter");
  if (filter) {
    filter.addEventListener("input", function () {
      var q = (filter.value || "").toLowerCase();
      document.querySelectorAll("#people .ucard").forEach(function (c) {
        c.style.display = c.dataset.search.includes(q) ? "" : "none";
      });
    });
  }
})();
