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

  // Overview: refresh each calendar with a live spinner / tick / cross, and a Stop button.
  var live = document.querySelector("form[data-live-refresh]");
  var rows = document.querySelectorAll("tr[data-cid]");
  if (live && rows.length && window.fetch && window.AbortController) {
    live.addEventListener("submit", function (e) {
      e.preventDefault();
      e.stopImmediatePropagation();
      var btn = live.querySelector("button");
      var csrf = live.querySelector("input[name=csrf]").value;
      var box = document.querySelector(".refresh-progress");
      var text = box.querySelector(".rp-text");
      var stop = box.querySelector(".rp-stop");
      var ctrl = new AbortController();
      var queue = Array.prototype.slice.call(rows);
      var total = queue.length, done = 0, failed = 0, stopped = false;
      btn.disabled = true;
      box.hidden = false;
      stop.hidden = false;
      stop.disabled = false;
      var tell = function () {
        text.textContent = stopped ? "Stopped: " + done + " of " + total + " checked"
          : done < total ? "Checking " + (done + 1 > total ? total : done + 1) + " of " + total + "\u2026"
          : (failed ? failed + " problem" + (failed === 1 ? "" : "s") : "All calendars OK");
      };
      queue.forEach(function (tr) {
        tr.querySelector(".st").innerHTML = '<span class="spin" aria-label="Checking"></span>';
        var err = document.querySelector('tr[data-err="' + tr.getAttribute("data-cid") + '"]');
        if (err) err.remove();
      });
      tell();
      stop.onclick = function () {
        stopped = true; ctrl.abort(); stop.hidden = true;
        queue.forEach(function (tr) { tr.querySelector(".st").innerHTML = '<span class="status wait">Skipped</span>'; });
        queue = [];
        tell(); btn.disabled = false;
      };
      var finish = function (tr, data) {
        var st = tr.querySelector(".st");
        if (data.ok) {
          st.innerHTML = '<span class="status ok">\u2713 OK</span>';
        } else {
          failed += 1;
          st.innerHTML = '<span class="status bad">\u2717 Problem</span>';
          var row = document.createElement("tr");
          row.className = "errrow";
          var td = document.createElement("td");
          td.colSpan = 4;
          td.textContent = data.error || "Couldn't download this calendar.";
          row.appendChild(td);
          tr.parentNode.insertBefore(row, tr.nextSibling);
        }
        if (data.count !== undefined) tr.querySelector(".cnt").textContent = data.count;
        if (data.updated) tr.querySelector(".upd").textContent = data.updated;
      };
      var next = function () {
        var tr = queue.shift();
        if (!tr) return Promise.resolve();
        return fetch("/api/refresh/" + encodeURIComponent(tr.getAttribute("data-cid")), {
          method: "POST", headers: { "X-CSRF": csrf }, signal: ctrl.signal, credentials: "same-origin"
        }).then(function (r) { return r.json(); })
          .then(function (data) { finish(tr, data); })
          .catch(function (err) { if (err.name !== "AbortError") finish(tr, { ok: false, error: "No answer from Glimpse." }); })
          .then(function () { if (!stopped) { done += 1; tell(); } return next(); });
      };
      Promise.all([next(), next(), next()]).then(function () {
        if (!stopped) { stop.hidden = true; btn.disabled = false; tell(); }
      });
    }, true);
  }

  // Select a link's text when its box is clicked, for easy manual copying.
  document.addEventListener("focusin", function (e) {
    if (e.target.matches(".copyrow input[readonly]")) e.target.select();
  });
})();
