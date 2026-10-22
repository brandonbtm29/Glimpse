// Click (or tap, or Enter) an event to see all of its details in a small window.
// Only shows what the page already includes for this viewer; nothing is fetched.
(function () {
  var dlg;
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }
  function build() {
    dlg = el("dialog", "evdlg");
    dlg.setAttribute("aria-label", "Event details");
    dlg.addEventListener("click", function (e) { if (e.target === dlg) dlg.close(); });
    document.body.appendChild(dlg);
  }
  function show(data) {
    if (!dlg) build();
    dlg.textContent = "";
    var card = el("div", "evdlg-card" + (/^[a-z0-9 -]+$/.test(data.cls || "") ? " " + data.cls : ""));
    if (data.color) card.style.setProperty("--ev", data.color);
    var head = el("div", "evdlg-head");
    var title = el("h2", null, (data.mark ? data.mark + " " : "") + (data.title || "Busy"));
    var close = el("button", "evdlg-x", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Close");
    close.addEventListener("click", function () { dlg.close(); });
    head.appendChild(title);
    head.appendChild(close);
    card.appendChild(head);
    card.appendChild(el("p", "evdlg-when", data.when));
    if (data.owner) card.appendChild(el("p", "evdlg-sub", data.owner));
    if (data.loc) card.appendChild(el("p", "evdlg-loc", data.loc));
    if (data.cal) card.appendChild(el("p", "evdlg-sub", data.cal));
    if (data.notes) card.appendChild(el("p", "evdlg-notes", data.notes));
    dlg.appendChild(card);
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
  }
  function open(target) {
    var node = target.closest && target.closest("[data-ev]");
    if (!node) return false;
    try { show(JSON.parse(node.getAttribute("data-ev"))); } catch (e) { return false; }
    return true;
  }
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("a")) return; // links (e.g. "+2 more") keep working
    if (open(e.target)) e.preventDefault();
  });
  document.addEventListener("keydown", function (e) {
    if ((e.key === "Enter" || e.key === " ") && e.target.hasAttribute && e.target.hasAttribute("data-ev")) {
      e.preventDefault();
      open(e.target);
    }
  });
})();
