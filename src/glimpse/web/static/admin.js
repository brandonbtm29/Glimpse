// Settings panel helpers: copy buttons and "are you sure?" prompts. No dependencies.
(function () {
  "use strict";

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (btn) {
      var text = btn.getAttribute("data-copy");
      var done = function () {
        var label = btn.textContent;
        btn.textContent = "Copied";
        btn.classList.add("copied");
        setTimeout(function () { btn.textContent = label; btn.classList.remove("copied"); }, 1600);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done);
      } else {
        var input = btn.parentElement.querySelector("input");
        if (input) { input.select(); document.execCommand("copy"); done(); }
      }
      return;
    }
    var confirmBtn = e.target.closest("button[data-confirm]");
    if (confirmBtn && !window.confirm(confirmBtn.getAttribute("data-confirm"))) {
      e.preventDefault();
    }
  });

  document.addEventListener("submit", function (e) {
    var form = e.target;
    var btn = e.submitter;
    if (!(btn && btn.hasAttribute("data-confirm"))) { // otherwise handled on click
      var msg = form.getAttribute("data-confirm");
      if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    }
    // Show progress; disable after this tick so the button's name/value is still sent.
    if (btn && !e.defaultPrevented) {
      setTimeout(function () { btn.disabled = true; btn.textContent = "Working\u2026"; }, 0);
    }
  });

  // Select a link's text when its box is clicked, for easy manual copying.
  document.addEventListener("focusin", function (e) {
    if (e.target.matches(".copyrow input[readonly]")) e.target.select();
  });
})();
