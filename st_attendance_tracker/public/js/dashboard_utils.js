function esc(s) { if (s===null||s===undefined) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

function getTaskIcon(s) {
  if (s === 'Done')        return 'ti-circle-check ico-g';
  if (s === 'In Progress') return 'ti-circle-half ico-b';
  return 'ti-circle ico-a';
}

function formatTimeHM(t) {
  if (!t) return '—';
  if (/AM|PM/i.test(t)) return t;
  var parts = String(t).split(':');
  if (parts.length >= 2) {
    var h = parseInt(parts[0], 10);
    var m = parseInt(parts[1], 10);
    if (isNaN(h) || isNaN(m)) return String(t);
    var ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12;
    h = h ? h : 12;
    var hStr = h < 10 ? '0' + h : h;
    var mStr = m < 10 ? '0' + m : m;
    return hStr + ':' + mStr + ' ' + ampm;
  }
  return String(t);
}
