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

  // Calendars page: drag calendars between group lanes (or use each card's menu).
  var board = document.querySelector(".board[data-csrf]");
  if (board && window.fetch) {
    var bcsrf = board.getAttribute("data-csrf");
    var status = document.querySelector(".board-status");
    var dragged = null;
    var say = function (msg) { if (status) status.textContent = msg; };
    var move = function (card, gid) {
      var lane = board.querySelector('.lane[data-gid="' + gid + '"] .lane-cards');
      if (!lane || card.closest(".lane").getAttribute("data-gid") === gid) return;
      var body = new FormData();
      body.append("cid", card.getAttribute("data-cid"));
      body.append("gid", gid);
      say("Saving\u2026");
      fetch("/api/calendar-group", { method: "POST", headers: { "X-CSRF": bcsrf }, body: body, credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.ok) { say(d.error || "Couldn't move it."); return; }
          lane.insertBefore(card, lane.querySelector(".lane-empty"));
          var sel = card.querySelector("select.move");
          if (sel) sel.value = gid;
          say("Saved.");
        })
        .catch(function () { say("Couldn't reach Glimpse."); });
    };
    board.addEventListener("dragstart", function (e) {
      dragged = e.target.closest(".cal-card");
      if (!dragged) return;
      dragged.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", dragged.getAttribute("data-cid"));
    });
    board.addEventListener("dragend", function () {
      if (dragged) dragged.classList.remove("dragging");
      board.querySelectorAll(".lane.over").forEach(function (l) { l.classList.remove("over"); });
      dragged = null;
    });
    board.addEventListener("dragover", function (e) {
      var lane = e.target.closest(".lane[data-gid]");
      if (!lane || !dragged) return;
      e.preventDefault();
      board.querySelectorAll(".lane.over").forEach(function (l) { if (l !== lane) l.classList.remove("over"); });
      lane.classList.add("over");
    });
    board.addEventListener("drop", function (e) {
      var lane = e.target.closest(".lane[data-gid]");
      if (!lane || !dragged) return;
      e.preventDefault();
      lane.classList.remove("over");
      move(dragged, lane.getAttribute("data-gid"));
    });
    board.addEventListener("change", function (e) {
      if (!e.target.matches("select.move")) return;
      move(e.target.closest(".cal-card"), e.target.value);
    });
  }

  // Select a link's text when its box is clicked, for easy manual copying.
  document.addEventListener("focusin", function (e) {
    if (e.target.matches(".copyrow input[readonly]")) e.target.select();
  });
})();

// Old links to an audience's #requests or #widget section open that tab.
(() => {
  const m = location.pathname.match(/^\/audiences\/[^/]+$/);
  const tab = { "#requests": "requests", "#widget": "widget", "#video-calls": "requests" }[location.hash];
  if (m && tab && !location.search.includes("tab=")) location.replace(`${location.pathname}?tab=${tab}${location.hash}`);
})();
