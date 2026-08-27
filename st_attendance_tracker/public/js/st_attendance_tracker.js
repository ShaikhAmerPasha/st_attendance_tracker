// Adds a persistent "Check-In" button to the Frappe Desk navbar beside Help/User menu
(function () {
  function injectCheckinButton() {
    if (document.getElementById('st-checkin-nav-item')) return;

    var anchor = document.querySelector('header .dropdown-help') || 
                 document.querySelector('header .dropdown-navbar-user') ||
                 document.querySelector('.dropdown-navbar-user') ||
                 document.querySelector('.dropdown-help');

    if (!anchor || !anchor.parentNode) return;

    var li = document.createElement('li');
    li.id = 'st-checkin-nav-item';
    li.className = 'nav-item d-flex align-items-center';
    li.style.cssText = 'margin-right: 8px; display: inline-flex; align-items: center; z-index: 1000;';

    li.innerHTML = 
      '<a id="st-checkin-nav-btn" href="/daily-checkin" title="Daily Check-In" ' +
      'style="display:inline-flex;align-items:center;gap:6px;background:#2490ef;color:#ffffff !important;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700;text-decoration:none;box-shadow:0 2px 4px rgba(36,144,239,0.3);transition:all 0.2s ease;line-height:1.4;">' +
      '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 8v-2a2 2 0 0 0 -2 -2h-7a2 2 0 0 0 -2 2v12a2 2 0 0 0 2 2h7a2 2 0 0 0 2 -2v-2"></path><path d="M20 12h-13l3 -3m0 6l-3 -3"></path></svg>' +
      'Check-In' + 
      '</a>';

    anchor.parentNode.insertBefore(li, anchor);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectCheckinButton);
  } else {
    injectCheckinButton();
  }

  window.addEventListener('load', injectCheckinButton);

  if (window.jQuery) {
    window.jQuery(document).on('toolbar_setup page-change app_ready', injectCheckinButton);
  }

  if (window.frappe) {
    if (frappe.ready) frappe.ready(injectCheckinButton);
    if (frappe.router) frappe.router.on('change', injectCheckinButton);
  }

  setInterval(injectCheckinButton, 300);
})();
