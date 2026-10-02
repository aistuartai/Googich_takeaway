// Menus (Configuration, Help, run Options) close when you click anywhere else on the page or
// press Escape. They are <details> elements, so they still open and close without this script.
(function () {
  "use strict";
  var MENUS = "details.nav-menu[open], details.options[open]";

  function closeAll(except) {
    document.querySelectorAll(MENUS).forEach(function (menu) {
      if (menu !== except && !menu.contains(except)) {
        menu.open = false;
      }
    });
  }

  document.addEventListener("click", function (event) {
    var inside = event.target.closest("details.nav-menu, details.options");
    closeAll(inside);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    var open = document.querySelector(MENUS);
    if (open) {
      closeAll(null);
      var summary = open.querySelector("summary");
      if (summary) {
        summary.focus();
      }
    }
  });
})();
