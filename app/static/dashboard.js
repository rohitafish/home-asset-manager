// The handful of behaviours the templates used to carry as inline
// onchange/onsubmit/onclick attributes. Moved here so the Content-Security-
// Policy (app/main.py) can drop 'unsafe-inline' from script-src: with that
// gone, no inline script anywhere in a page executes -- including one that
// might someday arrive through a rendered field -- only files served from
// /static. Delegated listeners, so they cover elements rendered later by
// htmx too.
(function () {
  "use strict";

  // <select data-autosubmit> inside a form: submit on change (filter bars).
  document.addEventListener("change", function (event) {
    var el = event.target;
    if (el && el.matches && el.matches("select[data-autosubmit]") && el.form) {
      if (el.form.requestSubmit) { el.form.requestSubmit(); } else { el.form.submit(); }
    }
  });

  // <form data-confirm="..."> : ask before a destructive POST.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form && form.matches && form.matches("form[data-confirm]")) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    }
  });

  // <button data-print> : print the page (the valuables register).
  document.addEventListener("click", function (event) {
    var button = event.target && event.target.closest ? event.target.closest("button[data-print]") : null;
    if (button) { window.print(); }
  });
})();
