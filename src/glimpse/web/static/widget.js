// Show the widget in the visitor's own time zone: reload once with ?tz=<their zone>.
(function () {
  var box = document.querySelector(".w");
  if (!box || box.getAttribute("data-has-tz") === "1" || !window.Intl) return;
  var zone;
  try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return; }
  if (zone) location.replace(box.getAttribute("data-self") + "?tz=" + encodeURIComponent(zone));
})();
