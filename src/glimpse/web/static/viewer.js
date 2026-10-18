// Optional: offer "Show your time" using the browser's time zone. Pages work without it.
(function () {
  var box = document.getElementById("zone");
  var link = box && box.querySelector(".yourzone");
  if (!link || !window.Intl) return;
  var zone;
  try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return; }
  if (!zone || zone === box.getAttribute("data-current")) return;
  link.href = link.getAttribute("data-here") + "?tz=" + encodeURIComponent(zone);
  link.textContent = "Show your time (" + zone.split("/").pop().replace(/_/g, " ") + ")";
  link.hidden = false;
})();

// Ask before buttons marked data-confirm (e.g. cancelling a meeting request).
document.addEventListener("submit", function (e) {
  var btn = e.submitter;
  var msg = btn && btn.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});
