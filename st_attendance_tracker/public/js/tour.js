// Thin wrapper around driver.js (loaded via CDN on the www/ pages that use
// a tour) so each page only needs to supply a tour id + step list.
var STTour = {
  start: function (tourId, steps) {
    if (typeof driver === 'undefined' || !driver.js) return;
    // Some pages render different DOM depending on state (e.g. daily-checkin
    // before/after checking in) — drop any step whose target isn't on the
    // page right now instead of letting driver.js choke on a missing element.
    var available = steps.filter(function (s) { return document.querySelector(s.element); });
    if (!available.length) return;
    driver.js.driver({
      showProgress: true,
      allowClose: true,
      steps: available,
      onDestroyed: function () { STTour.markSeen(tourId); }
    }).drive();
  },
  markSeen: function (tourId) {
    frappe.call({ method: 'st_attendance_tracker.tours.mark_tour_seen', args: { tour_id: tourId } });
  }
};
