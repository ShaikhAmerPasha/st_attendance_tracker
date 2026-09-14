/*
 * "What's New" notification bell + optional banner. Mount by adding
 *   <div class="wn-bell-wrap" id="wn-mount"></div>
 * to a page's nav, and (optionally, for the lighter-touch nudge)
 *   <div id="wn-banner-mount"></div>
 * right below the nav, then calling initWhatsNew() once on page load.
 * Fetches st_attendance_tracker.announcements.get_announcements, renders
 * the bell + dropdown panel, and marks everything seen when the panel is
 * opened. The banner is a session-local nudge only (dismissing it doesn't
 * mark anything seen server-side — opening the panel is what does that);
 * it's keyed by the newest unread announcement's name, so dismissing it
 * doesn't suppress a genuinely new one that arrives later.
 */
function initWhatsNew() {
  var mount = document.getElementById('wn-mount');
  if (!mount) return;
  var bannerMount = document.getElementById('wn-banner-mount');

  var ICONS = {
    Feature: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>',
    Improvement: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 19-7-7 7-7M5 12h14"/></svg>',
    Fix: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
    Report: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M3 9h6"/></svg>',
  };

  function iconClass(category) { return 'wn-ico wn-ico--' + (category || 'feature').toLowerCase(); }

  function relativeDate(dt) {
    var d = new Date((dt || '').replace(' ', 'T'));
    if (isNaN(d.getTime())) return '';
    var days = Math.floor((Date.now() - d.getTime()) / 86400000);
    if (days <= 0) return 'Today';
    if (days === 1) return 'Yesterday';
    if (days < 7) return days + ' days ago';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  mount.innerHTML =
    '<button class="wn-bell-btn" id="wn-bell" aria-label="What\'s new" aria-expanded="false">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>' +
    '</button>' +
    '<span class="wn-bell-dot" id="wn-dot" hidden></span>' +
    '<div class="wn-panel" id="wn-panel" role="dialog" aria-label="What\'s new" hidden>' +
      '<div class="wn-head"><div><h2>What\'s new</h2><span class="wn-sub" id="wn-sub"></span></div>' +
      '<button class="wn-close" id="wn-close" aria-label="Close"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>' +
      '<div class="wn-list" id="wn-list"></div>' +
    '</div>';

  var bell = document.getElementById('wn-bell');
  var dot = document.getElementById('wn-dot');
  var panel = document.getElementById('wn-panel');
  var sub = document.getElementById('wn-sub');
  var list = document.getElementById('wn-list');
  var closeBtn = document.getElementById('wn-close');
  var hasUnread = false;

  function render(items, unreadCount) {
    hasUnread = unreadCount > 0;
    dot.hidden = !hasUnread;
    sub.textContent = items.length
      ? (unreadCount > 0 ? unreadCount + ' new since your last visit' : 'You\'re all caught up')
      : '';
    if (!items.length) {
      list.innerHTML = '<div class="wn-empty">No announcements yet.</div>';
      return;
    }
    list.innerHTML = items.map(function (item) {
      var linkHtml = item.link
        ? '<a class="wn-link" href="' + item.link + '" target="_blank" rel="noopener">Learn more</a>'
        : '';
      return (
        '<div class="wn-item">' +
          '<div class="' + iconClass(item.category) + '">' + (ICONS[item.category] || ICONS.Feature) + '</div>' +
          '<div class="wn-body">' +
            '<div class="wn-title-row"><span class="wn-title">' + esc(item.title) + '</span>' +
              (item.is_new ? '<span class="wn-pill">New</span>' : '') + '</div>' +
            '<p class="wn-desc">' + esc(item.description) + '</p>' +
            '<div class="wn-meta"><span>' + relativeDate(item.publish_date) + '</span>' + linkHtml + '</div>' +
          '</div>' +
        '</div>'
      );
    }).join('');
  }

  function renderBanner(items, unreadCount) {
    if (!bannerMount) return;
    var newest = items.find(function (i) { return i.is_new; });
    if (!unreadCount || !newest) { bannerMount.innerHTML = ''; return; }

    var dismissKey = 'st_wn_banner_dismissed';
    if (sessionStorage.getItem(dismissKey) === newest.name) return;

    var text = unreadCount === 1
      ? '<strong>New:</strong> ' + esc(newest.title)
      : '<strong>' + unreadCount + ' new updates</strong> since your last visit — starting with ' + esc(newest.title);

    bannerMount.innerHTML =
      '<div class="wn-banner" id="wn-banner">' +
        '<div class="wn-banner-ico">' + (ICONS[newest.category] || ICONS.Feature) + '</div>' +
        '<div class="wn-banner-text">' + text + '</div>' +
        '<a class="wn-banner-link" href="#" id="wn-banner-link">See what\'s new</a>' +
        '<button class="wn-banner-x" id="wn-banner-x" aria-label="Dismiss">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>' +
        '</button>' +
      '</div>';

    document.getElementById('wn-banner-link').addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation(); // else the "click outside closes panel" document listener fires right after and undoes this
      bannerMount.innerHTML = '';
      setOpen(true);
    });
    document.getElementById('wn-banner-x').addEventListener('click', function () {
      try { sessionStorage.setItem(dismissKey, newest.name); } catch (e) {}
      bannerMount.innerHTML = '';
    });
  }

  function load() {
    frappe.call({
      method: 'st_attendance_tracker.announcements.get_announcements',
      callback: function (r) {
        if (!r.message) return;
        var items = r.message.items || [], unreadCount = r.message.unread_count || 0;
        render(items, unreadCount);
        renderBanner(items, unreadCount);
      },
    });
  }

  function setOpen(open) {
    panel.hidden = !open;
    bell.setAttribute('aria-expanded', String(open));
    if (open && hasUnread) {
      frappe.call({ method: 'st_attendance_tracker.announcements.mark_announcements_seen' });
      dot.hidden = true;
      hasUnread = false;
    }
  }

  bell.addEventListener('click', function () { setOpen(panel.hidden); });
  closeBtn.addEventListener('click', function () { setOpen(false); });
  document.addEventListener('click', function (e) {
    if (!panel.hidden && !mount.contains(e.target)) setOpen(false);
  });

  load();
}
