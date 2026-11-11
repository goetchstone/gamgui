// The Classroom screen (classroom.html): mark the picked class in the list, and remember it so a new search or page
// keeps it marked. The class ID is read off an autoescaped data-* attribute, never generated into JS.
(function () {
  "use strict";
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("#class-results [data-course]");
    if (!btn) return;
    document.querySelectorAll("#class-results [data-course]").forEach(function (b) {
      if (b === btn) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
    });
    var picked = document.getElementById("class-selected");
    if (picked) picked.value = btn.dataset.course;
  });
})();
