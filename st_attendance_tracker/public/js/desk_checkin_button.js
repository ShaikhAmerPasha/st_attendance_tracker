// Adds a "Check-In" button to the desk navbar, right next to Help, linking
// to the custom daily_checkin portal page.
frappe.ready(function () {
  var helpItem = document.querySelector('.navbar .dropdown-help');
  if (!helpItem || document.getElementById('st-checkin-nav-btn')) return;

  var li = document.createElement('li');
  li.className = 'nav-item d-none d-lg-block';
  li.innerHTML =
    '<a id="st-checkin-nav-btn" class="btn-reset nav-link" href="/daily-checkin" ' +
    'target="_blank" title="Daily Check-In"><span>' + __('Check-In') + '</span></a>';

  helpItem.parentNode.insertBefore(li, helpItem);
});
