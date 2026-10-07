/* Doorman UI — vanilla JS, no frameworks, offline LAN.
 * Talks to the in-process API in src/ui_api.py:
 *   GET  /api/status, /api/history, /api/sessions/{id}, /api/live,
 *        /api/snapshot/{id}, /api/config
 *   POST /api/config, /api/restart
 */
'use strict';

// ---------------------------------------------------------------- helpers
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.status;
    try { const j = await res.json(); if (j && j.error) detail = j.error; } catch (e) {}
    throw new Error('HTTP ' + detail);
  }
  return res.json();
}
const apiGet = p => api(p, { method: 'GET' });
const apiPost = (p, body) => api(p, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

function escText(s) { return s == null ? '' : String(s); }

// Display name for a session: recognized name when present, otherwise a
// trigger-aware label ("Doorbell press" for doorbell rings, the animal kind
// for cat/dog sessions, "Detected" for unrecognized person detection).
// Replaces the old blanket "Unknown".
function whoLabel(sum) {
  if (sum.recognized_name) return sum.recognized_name;
  if (sum.label === 'cat' || sum.label === 'dog') return sum.label;
  if (sum.trigger === 'doorbell') return 'Doorbell press';
  if (sum.trigger === 'person' || sum.trigger === 'animal') return 'Detected';
  return 'Unknown';
}

// Household timezone. Every timestamp in the UI is rendered in this zone, so
// history reads the same regardless of which device/LAN it's opened from.
// Keep in sync with TZ in docker-compose.yml / the Dockerfile.
const HOUSE_TZ = 'America/Los_Angeles';

function _laDate(iso) {
  const d = new Date(iso);
  return isNaN(d.getTime()) ? null : d;
}
function _laTodayKey() {
  return new Intl.DateTimeFormat('en-US', { timeZone: HOUSE_TZ, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
}
function _laDayKey(d) {
  return new Intl.DateTimeFormat('en-US', { timeZone: HOUSE_TZ, year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
}
function _laTime(d) {
  return new Intl.DateTimeFormat('en-US', { timeZone: HOUSE_TZ, hour: 'numeric', minute: '2-digit', hour12: true }).format(d);
}
function _laDayShort(d) {
  return new Intl.DateTimeFormat('en-US', { timeZone: HOUSE_TZ, month: 'short', day: 'numeric' }).format(d);
}

// Absolute timestamp in the household zone. opts.alwaysDate forces the date
// (used for history review); otherwise same-day-in-HOUSE_TZ shows time only.
function fmtWhen(iso, opts) {
  const d = _laDate(iso);
  if (!d) return '—';
  const sameDay = _laDayKey(d) === _laTodayKey();
  if (opts && opts.alwaysDate) return _laDayShort(d) + ', ' + _laTime(d);
  if (sameDay) return _laTime(d);
  return _laDayShort(d) + ', ' + _laTime(d);
}

// Time-only in the household zone (for the live transcript's per-line labels).
function fmtTime(iso) {
  const d = _laDate(iso);
  return d ? _laTime(d) : '';
}

// Full absolute timestamp in the household zone (history/session detail):
// "Wed, Sep 30, 2026, 9:49 PM".
function fmtFull(iso) {
  const d = _laDate(iso);
  if (!d) return '—';
  const s = new Intl.DateTimeFormat('en-US', {
    timeZone: HOUSE_TZ, weekday: 'short', month: 'short', day: 'numeric',
    year: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true,
  }).format(d);
  return s;
}


function fmtDur(s) {
  if (s == null) return '';
  s = Math.round(s);
  if (s < 60) return s + 's';
  return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
}

function fmtUptime(s) {
  if (s == null) return '—';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h > 0) return h + 'h ' + m + 'm';
  if (m > 0) return m + 'm ' + (s % 60) + 's';
  return s + 's';
}

let toastTimer = null;
function toast(msg, kind) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
}

// ---------------------------------------------------------------- state
const view = $('#view');
let currentTab = null;
let sessionParent = 'dashboard';   // which list a session was opened from (dashboard | history)

// ---------------------------------------------------------------- dashboard
function healthDotClass(s) {
  if (s.in_conversation) return 'live';
  if (s.camera_healthy === true) return 'ok';
  if (s.camera_healthy === false) return 'err';
  return 'warn'; // null = probe unknown
}

function goBack() {
  // history.back() when we arrived via a link; else fall back to the list.
  if (typeof history !== 'undefined' && history.length > 1) history.back();
  else location.hash = '#history';
}

function msgNode(m) {
  const role = m.role || 'system';
  const who = { visitor: 'Visitor', doorman: 'Doorman', tool: 'Tool', system: 'System' }[role] || role;
  const label = who + (m.kind ? ' · ' + m.kind : '') + ' · ' + fmtTime(m.ts);
  return h('div', { class: 'msg ' + role },
    h('div', { class: 'who', text: label }),
    h('div', { class: 'bubble', text: escText(m.text) }),
  );
}

function summaryLine(sum) {
  if (!sum) return 'No visits yet';
  // duration · msg-count. Shown in the "Last visit" card meta, next to the
  // separate `.when` timestamp field (so the time is not repeated here).
  return (sum.duration_s != null ? fmtDur(sum.duration_s) + ' · ' : '') +
    sum.message_count + ' msg';
}

async function renderDashboard() {
  let s;
  try { s = await apiGet('/api/status'); }
  catch (e) {
    view.replaceChildren(h('div', { class: 'muted-line', text: 'Status unavailable: ' + e.message }));
    return;
  }
  $('#status-dot').className = 'dot ' + healthDotClass(s);

  const cam = s.camera_healthy === true ? 'OK' : s.camera_healthy === false ? 'down' : 'unknown';
  const camCls = s.camera_healthy === true ? 'ok' : s.camera_healthy === false ? 'err' : 'warn';

  const top = $('#top-status');
  top.textContent = s.version + ' · ' + s.engine + ' · up ' + fmtUptime(s.uptime_s);

  // children can be null (idle -> no live banner / no live transcript);
  // replaceChildren(null) would append a literal "null" text node.
  view.replaceChildren(...[
    s.in_conversation && s.live_session
      ? h('div', { class: 'banner live' },
          h('span', { text: '●' }),
          h('span', { text: 'Live conversation in progress' }),
          h('div', { class: 'spacer' }),
          h('a', { href: '#live', class: 'open-live' }, 'Open')
        )
      : h('div', { class: 'banner none' }, h('span', { text: 'Idle — waiting for the next ring' })),

    h('div', { class: 'cards' },
      h('div', { class: 'card' },
        h('div', { class: 'k', text: 'Camera' }),
        h('div', { class: 'v ' + camCls, text: cam })),
      h('div', { class: 'card' },
        h('div', { class: 'k', text: 'Voice engine' }),
        h('div', { class: 'v', text: s.engine })),
      h('div', { class: 'card' },
        h('div', { class: 'k', text: 'Trigger' }),
        h('div', { class: 'v', text: s.trigger_mode })),
      h('div', { class: 'card' },
        h('div', { class: 'k', text: 'Uptime' }),
        h('div', { class: 'v', text: fmtUptime(s.uptime_s) })),
    ),

    h('div', { class: 'section-title', text: 'Last visit' }),
    h('div', { class: 'hist-list' },
      s.last_visit
        ? h('a', { href: '#session/' + s.last_visit.session_id, class: 'hist-item' },
            h('div', { class: 'when', text: fmtWhen(s.last_visit.started_at, { alwaysDate: true }) }),
            h('div', { class: 'name' },
              s.last_visit.recognized_name
                ? s.last_visit.recognized_name
                : h('span', { class: 'tag', text: whoLabel(s.last_visit) })),
            h('div', { class: 'meta', text: summaryLine(s.last_visit) }),
            s.last_visit.has_snapshot
              ? h('img', { class: 'snap', src: '/api/snapshot/' + s.last_visit.session_id, alt: 'snapshot',
                          loading: 'lazy' })
              : null)
        : h('div', { class: 'hist-empty', text: 'No visits recorded yet.' })),

    s.in_conversation && s.live_session
      ? (h('div', { class: 'section-title', text: 'Live transcript' }),
         h('div', { class: 'livebox' },
           h('div', { class: 'head' },
             h('span', { text: '●' }),
             h('span', { text: 'session ' + s.live_session.session_id +
               (s.live_session.recognized_name ? ' · ' + s.live_session.recognized_name : '') })),
           h('div', { class: 'transcript' }, (s.live_session.messages || []).map(msgNode))))
      : null,
  ].filter(Boolean));
}

// ---------------------------------------------------------------- history
// Page-size options for the history view (sessions per page). 'All' is not a
// page size but an incremental mode: sessions lazy-load in batches as the
// user scrolls (the old "Load more" behavior, automatic). The choice
// persists per browser via localStorage.
const HIST_PAGE_SIZES = [5, 10, 25, 50, 100, 'all'];
const HIST_ALL_BATCH = 50;   // rows fetched per scroll-triggered batch in "All"
let histState = { page: 1, pageSize: 5, total: 0, allLoaded: 0 };
let histObserver = null;      // IntersectionObserver for the "All" sentinel
let allLoading = false;       // guard against overlapping "All" batch fetches

function loadHistPageSize() {
  let v = null;
  try { v = localStorage.getItem('doorman.histPageSize'); } catch (e) {}
  if (v === 'all') return 'all';
  const n = Number(v);
  return HIST_PAGE_SIZES.includes(n) ? n : 5;
}

function saveHistPageSize(v) {
  try { localStorage.setItem('doorman.histPageSize', String(v)); } catch (e) {}
}

function sentinelInView() {
  const s = $('.hist-sentinel', view);
  if (!s) return false;
  const r = s.getBoundingClientRect();
  return r.top < window.innerHeight && r.bottom > 0;
}

function histItem(sum) {
  const who = whoLabel(sum);
  histRegistry[sum.session_id] = sum;   // availability math for "Export selected"
  const row = h('a', { href: '#session/' + sum.session_id, class: 'hist-item' },
    h('input', { class: 'hist-check', type: 'checkbox',
      title: 'Select for export',
      onclick(ev) {
        ev.preventDefault();   // we own the visual state (set .checked below)
        ev.stopPropagation();  // stop the row's #session link from firing
        const on = !ev.target.checked;   // old value -> desired new value
        ev.target.checked = on;
        if (on) exportSelection.add(sum.session_id);
        else exportSelection.delete(sum.session_id);
        updateExportSelectedBtn();
      } }),
    h('div', { class: 'when', text: fmtWhen(sum.started_at, { alwaysDate: true }) }),
    h('div', { class: 'name' }, who),
    h('div', { class: 'meta', text:
      (sum.duration_s != null ? fmtDur(sum.duration_s) + ' · ' : '') + sum.message_count + ' msg' }),
    sum.has_snapshot
      ? h('img', { class: 'snap', src: '/api/snapshot/' + sum.session_id, alt: 'snapshot',
                  loading: 'lazy',
                  onerror() { this.replaceWith(h('span', { class: 'snap', style: 'display:block' })); } })
      : null,
    h('div', { class: 'row-actions' },
      h('button', { class: 'del-btn', title: 'Export this visit', text: '⬇',
        onclick(ev) {
          ev.preventDefault();
          ev.stopPropagation();
          doExportSession(sum.session_id);
        } }),
      h('button', { class: 'del-btn', title: 'Delete this visit', text: '✕',
        onclick(ev) {
          ev.preventDefault();
          ev.stopPropagation();
          if (!confirm('Delete this visit?\n' + who + ' · ' +
                       fmtWhen(sum.started_at, { alwaysDate: true }) +
                       '\nTranscript and snapshot are removed. This cannot be undone.')) return;
          api('/api/sessions/' + encodeURIComponent(sum.session_id), { method: 'DELETE' })
            .then(() => { toast('Visit deleted.', 'ok'); exportSelection.delete(sum.session_id);
                         updateExportSelectedBtn(); renderHistory(); })
            .catch(e => toast('Delete failed: ' + e.message, 'err'));
        } }),
    ),
  );
  return row;
}

// "All" mode: append the next batch at offset `allLoaded`; reset=true starts
// at the top of the list.
async function loadAllPage(reset) {
  if (allLoading) return;
  allLoading = true;
  let more = false;
  try {
    if (reset) {
      histState.allLoaded = 0;
      const list = $('.hist-list', view);
      if (list) list.replaceChildren();
    }
    const d = await apiGet('/api/history?limit=' + HIST_ALL_BATCH + '&offset=' + histState.allLoaded);
    histState.total = d.total;
    const list = $('.hist-list', view) || view;
    for (const sum of d.sessions) list.append(histItem(sum));
    histState.allLoaded += d.sessions.length;
    if (!histState.allLoaded && reset) {
      list.append(h('div', { class: 'hist-empty', text: 'No visits recorded yet.' }));
    }
    updateHistPager();
    if (histState.allLoaded >= histState.total) {
      // nothing left to lazy-load: stop observing and drop the sentinel.
      if (histObserver) { histObserver.disconnect(); histObserver = null; }
      const sent = $('.hist-sentinel', view);
      if (sent) sent.remove();
    } else {
      // If the sentinel is still on screen (this batch didn't fill the
      // viewport), immediately fetch the next one so the list keeps filling.
      more = sentinelInView();
    }
  } catch (e) {
    // `list` may be undefined if the fetch threw before it was declared, so
    // resolve it here rather than relying on the try-block binding.
    ($('.hist-list', view) || view).append(
      h('div', { class: 'muted-line', text: 'History unavailable: ' + e.message }));
  } finally {
    allLoading = false;
    if (more) loadAllPage(false);
  }
}

// Numbered modes (5/10/25/50/100): replace the list with one page.
async function loadHistory() {
  const limit = histState.pageSize;
  let offset = (histState.page - 1) * limit;
  try {
    let d = await apiGet('/api/history?limit=' + limit + '&offset=' + offset);
    histState.total = d.total;
    // Clamp forward drift: if the total shrank under us (deletes), don't sit
    // on a page that no longer exists.
    const pages = Math.max(1, Math.ceil(histState.total / limit));
    if (histState.page > pages) {
      histState.page = pages;
      offset = (histState.page - 1) * limit;
      if (offset > 0) {
        d = await apiGet('/api/history?limit=' + limit + '&offset=' + offset);
        histState.total = d.total;
      }
    }
    const list = $('.hist-list', view) || view;
    list.replaceChildren();
    if (!d.sessions.length) {
      list.append(h('div', { class: 'hist-empty', text: 'No visits recorded yet.' }));
    } else {
      for (const sum of d.sessions) list.append(histItem(sum));
    }
    updateHistPager();
  } catch (e) {
    // `list` may be undefined if the fetch threw before it was declared, so
    // resolve it here rather than relying on the try-block binding.
    ($('.hist-list', view) || view).append(
      h('div', { class: 'muted-line', text: 'History unavailable: ' + e.message }));
  }
}

function updateHistPager() {
  // Mirror the readout + arrow state on both the top and bottom pager rows.
  for (const pf of ['top', 'bottom']) {
    const count = document.getElementById('hist-count-' + pf);
    if (!count) continue;
    const prev = document.getElementById('hist-prev-' + pf);
    const next = document.getElementById('hist-next-' + pf);
    if (histState.pageSize === 'all') {
      // lazy mode: show how many are loaded vs total; arrows don't apply.
      count.textContent = histState.allLoaded >= histState.total
        ? histState.total + (histState.total === 1 ? ' session' : ' sessions')
        : 'showing ' + histState.allLoaded + ' of ' + histState.total + ' sessions';
      if (prev) prev.disabled = true;
      if (next) next.disabled = true;
      continue;
    }
    const pages = Math.max(1, Math.ceil(histState.total / histState.pageSize));
    const p = Math.min(histState.page, pages);
    count.textContent = 'Page ' + p + ' of ' + pages + ' · ' + histState.total +
      (histState.total === 1 ? ' session' : ' sessions');
    if (prev) prev.disabled = p <= 1;
    if (next) next.disabled = p >= pages;
  }
}

function setupHistObserver() {
  if (histObserver) histObserver.disconnect();
  let sent = $('.hist-sentinel', view);
  if (!sent) {
    sent = h('div', { class: 'hist-sentinel', text: 'Loading…' });
    // keep the sentinel above the bottom pager row so it stays the last thing
    // the user scrolls to (the bottom row is the final child of `view`).
    const bottom = view.lastElementChild;
    view.insertBefore(sent, bottom || null);
  }
  // rootMargin pre-fetches the next batch ~400px before the sentinel reaches
  // the viewport, so the list feels continuous instead of stalling.
  histObserver = new IntersectionObserver(entries => {
    for (const en of entries) {
      if (en.isIntersecting && histState.pageSize === 'all' && !allLoading) loadAllPage(false);
    }
  }, { rootMargin: '400px 0px' });
  histObserver.observe(sent);
}

// Keep every page-size <select> in the DOM showing the current choice (there
// are two: top and bottom of the list).
function syncPageSizeSelects() {
  $$('.page-size', view).forEach(sel => { sel.value = String(histState.pageSize); });
}

// One pager row. `pf` ('top' | 'bottom') namespaces the element ids so both
// rows can be updated independently by updateHistPager().
function histPagerRow(pf) {
  const sel = h('select', { class: 'page-size', title: 'Sessions per page' },
    HIST_PAGE_SIZES.map(v => h('option', { value: String(v), text: v === 'all' ? 'All' : String(v) })));
  sel.value = String(histState.pageSize);
  sel.addEventListener('change', () => {
    histState.pageSize = sel.value === 'all' ? 'all' : Number(sel.value);
    saveHistPageSize(histState.pageSize);
    syncPageSizeSelects();   // keep the top/bottom selects in agreement
    histState.page = 1;   // changing page size always returns to page 1
    histState.allLoaded = 0;
    const list = $('.hist-list', view);
    if (list) list.replaceChildren();
    const oldSent = $('.hist-sentinel', view);
    if (histState.pageSize === 'all') {
      if (oldSent) oldSent.remove();
      setupHistObserver();
      loadAllPage(true);
    } else {
      if (histObserver) { histObserver.disconnect(); histObserver = null; }
      if (oldSent) oldSent.remove();
      loadHistory();
    }
  });
  return h('div', { class: 'hist-pager' + (pf === 'bottom' ? ' bottom' : '') },
    h('span', { class: 'hint', text: 'Per page' }),
    sel,
    h('div', { class: 'spacer' }),
    h('button', { class: 'page-btn', id: 'hist-prev-' + pf, onclick: () => {
      if (histState.pageSize !== 'all' && histState.page > 1) { histState.page--; loadHistory(); }
    } }, '← Prev'),
    h('button', { class: 'page-btn', id: 'hist-next-' + pf, onclick: () => {
      if (histState.pageSize === 'all') return;
      const pages = Math.max(1, Math.ceil(histState.total / histState.pageSize));
      if (histState.page < pages) { histState.page++; loadHistory(); }
    } }, 'Next →'),
    h('span', { class: 'page-count', id: 'hist-count-' + pf, text: '…' }),
  );
}

function renderHistory() {
  histState = { page: 1, pageSize: loadHistPageSize(), total: 0, allLoaded: 0 };
  view.replaceChildren(
    h('div', { class: 'section-title' }, 'Visit history',
      h('button', { class: 'clear-all', onclick: clearAllHistory, text: 'Clear all' })),
    histPagerRow('top'),
    h('div', { class: 'hist-list' }),
    // a second pager row at the bottom of the list mirrors the top one, so
    // long lists don't require scrolling back up to change page/size.
    histPagerRow('bottom'));
  if (histState.pageSize === 'all') {
    setupHistObserver();
    loadAllPage(true);
  } else {
    loadHistory();
  }
}

async function clearAllHistory() {
  let all;
  try {
    all = await apiGet('/api/history?limit=all&offset=0');
  } catch (e) {
    toast('History unavailable: ' + e.message, 'err');
    return;
  }
  if (!all.total) { toast('Nothing to delete.', 'ok'); return; }
  if (!confirm('Delete ALL ' + all.total + ' visits?\nTranscripts and snapshots are removed. This cannot be undone.')) return;
  try {
    const r = await api('/api/history', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ count: all.total }),
    });
    toast('Deleted ' + r.deleted + ' visits.', 'ok');
    renderHistory();
  } catch (e) {
    if (e.message && e.message.indexOf('count changed') !== -1) {
      toast('History changed since the warning; try again.', 'err');
      renderHistory();
    } else {
      toast('Delete failed: ' + e.message, 'err');
    }
  }
}

// Session media pane: the Frigate clip is the primary media. When the clip
// 404s (no matching Frigate event with a clip), the snapshot is shown
// instead. Links keep both media reachable without reloading the page.
function clipFallbackNode(s) {
  const clipUrl = '/api/clip/' + encodeURIComponent(s.session_id);
  const snapUrl = s.snapshot ? '/api/snapshot/' + encodeURIComponent(s.session_id) : null;
  const snapFallback = snapUrl
    ? h('img', { class: 'snap-fallback', src: snapUrl, alt: 'Snapshot',
                 onerror() { this.replaceWith(h('div', { class: 'no-snap', text: 'Snapshot unavailable' })); } })
    : null;
  return h('div', { class: 'clip-pane' },
    h('video', { controls: true, preload: 'metadata',
                 poster: snapUrl || '',
                 src: clipUrl,
                 onerror() {
                   // Only swap on MEDIA_ERR_SRC_NOT_FOUND (code 4), which is
                   // what our /api/clip 404 produces. A transient 502 (Frigate
                   // unreachable) or a decode error maps to a different code,
                   // so we keep the player and a reload can retry.
                   const code = this.error ? this.error.code : 0;
                   if (code !== 4) return;
                   const pane = this.parentElement;
                   this.replaceWith(snapFallback || h('div', { class: 'no-snap', text: 'No clip or snapshot' }));
                   const actions = pane.querySelector('.clip-actions');
                   if (actions) actions.remove();
                 } }),
    h('div', { class: 'clip-actions' },
      h('a', { class: 'clip-dl', href: clipUrl, target: '_blank', text: 'Open clip in new tab' }),
      snapUrl
        ? h('a', { class: 'clip-dl', href: snapUrl, target: '_blank', text: 'Open snapshot' })
        : h('span', { class: 'clip-dl', text: 'No snapshot' })
    )
  );
}

// ---------------------------------------------------------------- session detail
async function renderSession(id) {
  view.replaceChildren(h('div', { class: 'muted-line', text: 'Loading…' }));
  let s;
  try { s = await apiGet('/api/sessions/' + encodeURIComponent(id)); }
  catch (e) {
    view.replaceChildren(
      h('button', { class: 'back', onclick: () => location.hash = '#history' }, '← Back'),
      h('div', { class: 'muted-line', text: 'Session not found: ' + e.message }));
    return;
  }
  const when = s.started_at ? fmtFull(s.started_at) : '';
  view.replaceChildren(
    h('div', { class: 'detail' },
      h('div', { class: 'head' },
        h('button', { class: 'back', onclick: goBack }, '←'),
        h('h2', { text: whoLabel(s) }),
        h('span', { class: 'meta', text:
          when +
          (s.status ? ' · ' + s.status : '') +
          (s.duration_s != null ? ' · ' + fmtDur(s.duration_s) : '') }),
        h('button', { class: 'del-btn', style: 'margin-left:auto', text: 'Export',
          onclick: () => doExportSession(s.session_id) }),
        h('button', { class: 'del-btn', text: 'Delete',
          onclick: async () => {
            if (!confirm('Delete this visit?\nTranscript and snapshot are removed. This cannot be undone.')) return;
            try {
              await api('/api/sessions/' + encodeURIComponent(s.session_id), { method: 'DELETE' });
              toast('Visit deleted.', 'ok');
              location.hash = sessionParent === 'dashboard' ? '#dashboard' : '#history';
            } catch (e) {
              if (e.message && e.message.indexOf('HTTP 409') !== -1)
                toast('This session is live right now; delete it after it ends.', 'err');
              else
                toast('Delete failed: ' + e.message, 'err');
            }
          } }),
      ),
      h('div', { class: 'body' },
        h('div', { class: 'media-pane' }, clipFallbackNode(s)),
        (s.messages && s.messages.length)
          ? h('div', { class: 'transcript' }, s.messages.map(msgNode))
          : h('div', { class: 'transcript' }, h('div', { class: 'muted-line', text: 'No transcript.' }))
      )
    ),
  );
  view.querySelector('.transcript')?.scrollTo(0, 0);
}

// ---------------------------------------------------------------- live
async function renderLive() {
  view.replaceChildren(h('div', { class: 'muted-line', text: 'Loading…' }));
  const s = await apiGet('/api/live');
  if (!s) {
    view.replaceChildren(
      h('div', { class: 'banner none' }, h('span', { text: 'No live conversation right now.' })),
      h('a', { class: 'back', href: '#dashboard', style: 'margin-top:12px;display:inline-block' }, '← Dashboard'));
    return;
  }
  const box = h('div', { class: 'livebox' },
    h('div', { class: 'head' },
      h('span', { text: '●' }),
      h('span', { text: 'Live · ' + s.session_id +
        (s.recognized_name ? ' · ' + s.recognized_name : '') +
        ' · ' + (s.trigger || '') })),
    h('div', { class: 'transcript' }),
  );
  const tr = $('.transcript', box);
  view.replaceChildren(
    h('button', { class: 'back', style: 'margin-bottom:12px', onclick: goBack }, '←'),
    box,
  );
  const draw = () => {
    const msgs = (s.messages || []).map(msgNode);
    tr.replaceChildren(...msgs);   // spread: replaceChildren(array) would stringify it
    tr.scrollTo(0, tr.scrollHeight);
  };
  draw();
  timers.push(setInterval(async () => {
    try {
      const fresh = await apiGet('/api/live');
      if (!fresh) { location.hash = '#dashboard'; return; }
      s.messages = fresh.messages;
      draw();
    } catch (e) { /* live poll dropped; view re-renders on next route */ }
  }, 2000));
}

// ---------------------------------------------------------------- settings
// secrets: the server masks set secrets as '****'; re-send only when the user
// actually edited (or cleared) the field.
let settingsCache = null;   // {key: spec-dict + dirty}
let settingsInputs = {};    // {key: {input, spec}}
let promptsState = null;   // {key: {spec, input, dirty}}
// Which section's pane is open in the master-detail settings layout.
// buildSettings() re-runs on every Save (and on route entry); remembering the
// active section keeps the user on the same pane across that re-render.
let settingsActiveSection = null;
// Per-section 'Show advanced' toggles the user has opened, so a re-render
// keeps the tuning fields visible for sections they've expanded.
const advancedShown = new Set();

function buildSettings() {
  const fields = settingsCache ? Object.values(settingsCache) : [];
  const root = h('div', {});
  const searchInput = h('input', {
    type: 'search', id: 'settings-search', class: 'settings-search',
    placeholder: 'Search settings (label, key, or help…)', autocomplete: 'off',
  });
  const searchCount = h('span', { id: 'settings-search-count', class: 'settings-search-count' }, '');
  searchCount.hidden = true;
  const bar = h('div', { class: 'settings-bar' },
    searchInput,
    searchCount,
    h('span', { class: 'hint', id: 'dirty-hint', text: 'Changes apply live unless marked “restart”.' }),
    h('button', { class: 'btn ghost', id: 'revert-btn', onclick: revertSettings }, 'Revert'),
    h('button', { class: 'btn ghost', id: 'test-notify-btn', onclick: sendTestNotification }, 'Test notification'),
    h('button', { class: 'btn', id: 'save-btn', onclick: saveSettings }, 'Save'),
  );
  root.append(bar);

  const groups = {};
  for (const spec of fields) (groups[spec.group] = groups[spec.group] || []).push(spec);
  const groupNames = Object.keys(groups);

  // Master-detail: a left column of section links ("options") and a right
  // pane showing the selected section's fields. Clicking a link switches the
  // pane. Remembering settingsActiveSection keeps the user on the same pane
  // across the re-render buildSettings() does on Save.
  const layout = h('div', { class: 'settings-layout' });
  const nav = h('nav', { class: 'settings-nav' },
    h('span', { class: 'settings-nav-label', text: 'Settings' }));
  const pane = h('section', { class: 'settings-pane' });
  layout.append(nav, pane);

  const navItems = {};
  for (const gname of groupNames) {
    const btn = h('button', {
      class: 'settings-nav-link',
      onclick: () => setActiveSection(gname),
    });
    btn.append(document.createTextNode(gname));
    btn.append(h('span', { class: 'settings-nav-count', text: String(groups[gname].length) }));
    nav.append(btn);
    navItems[gname] = btn;
  }

  // Initial section: the remembered one if it still exists, else the first.
  if (!settingsActiveSection || !groupNames.includes(settingsActiveSection)) {
    settingsActiveSection = groupNames[0] || null;
  }
  renderActivePane(pane, groups);
  for (const gname of groupNames) {
    navItems[gname].classList.toggle('active', gname === settingsActiveSection);
  }
  root.append(layout);

  view.replaceChildren(root);
  updateDirtyHint();

  function setActiveSection(gname) {
    settingsActiveSection = gname;
    renderActivePane(pane, groups);
    for (const g of groupNames) navItems[g].classList.toggle('active', g === gname);
    applySettingsFilter();
  }

  // Render the selected section's fields into the right pane: normal rows,
  // then the rarely-tuned advanced fields behind a per-section toggle that
  // remembers its open state across re-renders (advancedShown).
  function renderActivePane(target, groups) {
    const gname = settingsActiveSection;
    const specs = gname ? groups[gname] : [];
    target.replaceChildren(h('h2', { class: 'settings-pane-title', text: gname || '' }));
    const body = h('div', { class: 'rows' });
    for (const spec of specs.filter(s => !s.advanced)) body.append(settingRow(spec));
    const adv = specs.filter(s => s.advanced);
    if (adv.length) {
      const advDetails = h('details', { class: 'advanced' },
        h('summary', { text: 'Show advanced (' + adv.length + ')' }),
        h('div', { class: 'rows adv-rows' }));
      const advRows = $('.adv-rows', advDetails);
      for (const spec of adv) advRows.append(settingRow(spec));
      advDetails.open = advancedShown.has(gname);
      advDetails.addEventListener('toggle', () => {
        advDetails.querySelector('summary').textContent =
          (advDetails.open ? 'Hide advanced (' : 'Show advanced (') + adv.length + ')';
        if (advDetails.open) advancedShown.add(gname); else advancedShown.delete(gname);
      });
      body.append(advDetails);
    }
    target.append(body);
  }

  // Live search: a query surfaces matching rows in the active pane, marks each
  // section's match count in the nav (dimming zero-match sections), and if the
  // active section has no match, jumps to the first section that does. Counts
  // come from the data model so every section is reflected, not just the one
  // currently rendered. A query bypasses the advanced toggle so tuning fields
  // stay findable; clearing it restores the active section's remembered state.
  function applySettingsFilter() {
    const q = (searchInput.value || '').trim();
    const matchByGroup = {};
    let total = 0, matched = 0;
    for (const gname of groupNames) {
      let secMatch = 0;
      for (const spec of groups[gname]) {
        total += 1;
        if (fieldMatchesQuery(spec, q)) { secMatch += 1; matched += 1; }
      }
      matchByGroup[gname] = secMatch;
      navItems[gname].classList.toggle('dim', !!q && secMatch === 0);
    }
    const prev = settingsActiveSection;
    if (q && matchByGroup[prev] === 0) {
      const first = groupNames.find(g => matchByGroup[g] > 0);
      if (first) settingsActiveSection = first;
    }
    if (settingsActiveSection !== prev && navItems[settingsActiveSection]) {
      renderActivePane(pane, groups);
      for (const g of groupNames) navItems[g].classList.toggle('active', g === settingsActiveSection);
    }
    $$('.row', pane).forEach(row => {
      const key = row.dataset.key;
      const spec = settingsCache ? settingsCache[key] : null;
      const isAdv = !!(spec && spec.advanced);
      const hit = fieldMatchesQuery(spec || { label: key, key: key, help: '' }, q);
      const visible = q ? hit : (isAdv ? advancedShown.has(settingsActiveSection) : true);
      row.hidden = !visible;
    });
    const advDetails = $('details.advanced', pane);
    if (advDetails) {
      const advHit = $$('.row', advDetails).some(r => !r.hidden);
      advDetails.open = q ? advHit : advancedShown.has(settingsActiveSection);
    }
    searchCount.hidden = !q;
    searchCount.textContent = q ? matched + ' of ' + total + ' settings' : '';
  }
  searchInput.addEventListener('input', applySettingsFilter);
}

function settingRow(spec) {
  const wrap = h('div', { class: 'row' });
  wrap.dataset.key = spec.key;   // lets the live search filter identify each row
  let input;
  if (spec.type === 'boolean') {
    input = h('input', { type: 'checkbox' });
    input.checked = !!spec.value;
    wrap.append(
      h('label', { text: spec.label }, spec.restart_required ? h('span', { class: 'badge', text: 'restart' }) : null),
      h('div', {},
        h('div', { class: 'boolwrap' }, input, h('span', { class: 'val', text: String(!!spec.value) })),
        spec.help ? h('div', { class: 'help', text: spec.help }) : null,
      ),
    );
    input.addEventListener('change', () => {
      $('.val', wrap).textContent = String(input.checked);
      markDirty(spec.key);
    });
  } else if (spec.type === 'select') {
    input = h('select', {});
    for (const opt of (spec.options || [])) {
      const o = h('option', { value: opt, text: opt });
      if (String(spec.value) === String(opt)) o.selected = true;
      input.append(o);
    }
    wrap.append(
      h('label', { text: spec.label }, spec.restart_required ? h('span', { class: 'badge', text: 'restart' }) : null),
      h('div', {},
        input,
        spec.help ? h('div', { class: 'help', text: spec.help }) : null,
      ),
    );
    input.addEventListener('change', () => markDirty(spec.key));
  } else {
    const type = spec.type === 'number' ? 'number'
      : spec.type === 'secret' ? 'password' : 'text';
    input = h('input', { type, value: spec.value == null ? '' : String(spec.value) });
    if (spec.type === 'number') {
      input.setAttribute('step', 'any');
      input.setAttribute('inputmode', 'decimal');
    }
    if (spec.type === 'secret') input.setAttribute('autocomplete', 'new-password');
    wrap.append(
      h('label', { text: spec.label }, spec.restart_required ? h('span', { class: 'badge', text: 'restart' }) : null),
      h('div', {},
        input,
        spec.help ? h('div', { class: 'help', text: spec.help }) : null,
      ),
    );
    input.addEventListener('input', () => markDirty(spec.key));
  }
  settingsInputs[spec.key] = { input, spec };
  return wrap;
}

function markDirty(key) {
  const rec = settingsInputs[key];
  if (!rec) return;
  rec.input.classList.add('dirty');
  settingsCache[key].dirty = true;
  updateDirtyHint();
}

function revertSettings() {
  if (!settingsCache) return;
  for (const key of Object.keys(settingsInputs)) {
    const rec = settingsInputs[key];
    const spec = settingsCache[key];
    settingsCache[key].dirty = false;
    rec.input.classList.remove('dirty');
    if (rec.spec.type === 'boolean') {
      rec.input.checked = !!spec.value;
      rec.input.closest('.boolwrap').querySelector('.val').textContent = String(rec.input.checked);
    } else if (rec.spec.type === 'select') {
      rec.input.value = String(spec.value == null ? '' : spec.value);
    } else {
      rec.input.value = spec.value == null ? '' : String(spec.value);
    }
  }
  updateDirtyHint();
}

async function sendTestNotification() {
  const btn = $('#test-notify-btn');
  if (btn) { btn.disabled = true; const prev = btn.textContent; btn.textContent = 'Sending…'; }
  try {
    const r = await apiPost('/api/notify-test', {});
    toast(r.ok ? 'Test notification sent — check your phone.'
               : 'Notification failed: ' + r.detail, r.ok ? 'ok' : 'err');
  } catch (e) {
    toast('Notification failed: ' + e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Test notification'; }
  }
}

function updateDirtyHint() {
  const n = Object.values(settingsCache || {}).filter(s => s.dirty).length;
  const hint = $('#dirty-hint');
  if (hint) hint.textContent = n
    ? n + ' field' + (n > 1 ? 's' : '') + ' changed — save to apply.'
    : 'Changes apply live unless marked “restart”.';
}

function fieldValue(key) {
  const rec = settingsInputs[key];
  const spec = rec.spec;
  if (spec.type === 'boolean') return rec.input.checked;
  if (spec.type === 'number') {
    const raw = rec.input.value.trim();
    if (raw === '') return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  }
  if (spec.type === 'secret') return rec.input.value;
  return rec.input.value.trim();
}

async function saveSettings() {
  // Only fields the user actually touched go out. That also means a masked
  // secret ('****') is never re-sent untouched — dirty is set on edit only.
  const dirtyKeys = Object.keys(settingsCache || {}).filter(k => settingsCache[k].dirty);
  if (!dirtyKeys.length) { toast('Nothing to save.'); return; }
  const values = {};
  for (const k of dirtyKeys) values[k] = fieldValue(k);
  const btn = $('#save-btn');
  btn.disabled = true;
  try {
    const r = await apiPost('/api/config', { values });
    for (const k of Object.keys(values)) {
      const rec = settingsInputs[k];
      const spec = settingsCache[k];
      spec.value = (spec.type === 'secret' && values[k] !== null && values[k] !== '') ? '****' : values[k];
      spec.dirty = false;
      rec.input.classList.remove('dirty');
    }
    buildSettings();
    const applied = (r.applied || []).length;
    if (r.needs_restart) {
      toast('Saved — restart required for ' +
        (r.restart_required || []).length + ' setting(s).', 'ok');
      if (confirm('Some settings need a restart to take effect (' +
        (r.restart_required || []).join(', ') +
        '). Restart Doorman now?')) restartNow();
    } else {
      toast('Saved' + (applied ? ' — applied live.' : '.'), 'ok');
    }
  } catch (e) {
    toast('Save failed: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

async function restartNow() {
  const ov = h('div', { class: 'overlay' },
    h('div', { class: 'box' },
      h('div', { class: 'spinner' }),
      h('div', { text: 'Restarting Doorman…' }),
      h('div', { style: 'color:var(--muted);font-size:13px', text: 'Back in ~2s' }),
    ));
  document.body.append(ov);
  try {
    await apiPost('/api/restart', {});
  } catch (e) { /* connection drops during re-exec — fine */ }
  const attempt = async () => {
    try {
      await apiGet('/api/status');
      location.reload();
    } catch (e) { setTimeout(attempt, 1500); }
  };
  setTimeout(attempt, 2000);
}

async function renderSettings() {
  view.replaceChildren(h('div', { class: 'muted-line', text: 'Loading settings…' }));
  settingsInputs = {};
  try {
    const r = await apiGet('/api/config');
    settingsCache = {};
    for (const f of r.fields) {
      settingsCache[f.key] = { ...f, dirty: false };
    }
  } catch (e) {
    view.replaceChildren(h('div', { class: 'muted-line', text: 'Settings unavailable: ' + e.message }));
    return;
  }
  buildSettings();
}

// ---------------------------------------------------------------- prompts
async function renderPrompts() {
  view.replaceChildren(h('div', { class: 'muted-line', text: 'Loading prompts…' }));
  promptsState = {};
  let d;
  try {
    d = await apiGet('/api/prompts');
  } catch (e) {
    view.replaceChildren(h('div', { class: 'muted-line', text: 'Prompts unavailable: ' + e.message }));
    return;
  }
  const bar = h('div', { class: 'settings-bar' },
    h('span', { class: 'hint', text: 'Edits apply on the next interaction (no restart).' }),
    h('button', { class: 'btn ghost', id: 'p-revert', onclick: pRevert }, 'Revert'),
    h('button', { class: 'btn', id: 'p-save', onclick: pSave }, 'Save'),
  );
  const root = h('div', {});
  root.append(bar);

  const LABELS = {
    system: 'Main system prompt',
    animal: 'Animal greeting prompt',
    animal_cat_lines: 'Cat one-liners (greeting pool)',
    animal_dog_lines: 'Dog one-liners (greeting pool)',
    animal_generic_line: 'Generic animal fallback line',
    trigger: 'Session trigger / prime line',
    local_notes: 'Local-engine notes (local LLM only)',
  };
  const HELP = {
    system: 'Tokens: {household_hint}, {identity}. This is the household-policy prompt used by both engines.',
    animal: 'Tokens: {animal_label}, {greeting}. {greeting} is auto-filled from the one-liner pools below.',
    animal_cat_lines: 'One greeting per line. A random line is picked as {greeting} when a cat is detected.',
    animal_dog_lines: 'One greeting per line. A random line is picked as {greeting} when a dog is detected.',
    animal_generic_line: 'Fallback line used when the detected label is not cat or dog.',
    trigger: 'Token: {facts} (auto-filled with doorbell / recognized / label context).',
    local_notes: 'Appended to the main prompt when DOORMAN_VOICE_ENGINE=local.',
  };

  for (const key of d.order) {
    const e = d.prompts[key];
    const card = h('div', { class: 'prompt-card' },
      h('div', { class: 'prompt-head' },
        h('span', { class: 'prompt-title', text: LABELS[key] || key }),
        (e.text !== e.default ? h('span', { class: 'tag', text: 'edited' }) : null),
        h('button', { class: 'btn ghost sm', onclick: () => pReset(key) }, 'Reset to default')),
      h('div', { class: 'help', text: HELP[key] || '' }),
      h('textarea', { class: 'prompt-area', spellcheck: 'false',
                      oninput: () => pDirty(key) }),
    );
    const ta = card.querySelector('.prompt-area');
    ta.value = e.text;
    promptsState[key] = { spec: e, input: ta, dirty: false };
    root.append(card);
  }
  root.append(h('div', { class: 'prompt-card' },
    h('div', { class: 'prompt-head' }, h('span', { class: 'prompt-title', text: 'Preview (rendered)' })),
    h('div', { class: 'help', text: 'What the main system prompt will actually be, for a recognized visitor vs an unknown one. Refreshes when you save.' })),
  );
  root.append(h('pre', { class: 'prompt-preview', id: 'p-preview-recognized', text: '…' }));
  root.append(h('pre', { class: 'prompt-preview', id: 'p-preview-unknown', text: '…' }));

  view.replaceChildren(root);
  pRefreshPreview();
}

function pDirty(key) {
  promptsState[key].dirty = true;
}

async function pRefreshPreview() {
  const sys = promptsState['system'] ? promptsState['system'].input.value : '';
  try {
    const r = await apiPost('/api/prompts/preview', { system: sys });
    const a = $('#p-preview-recognized'); if (a) a.textContent = r.system.recognized;
    const b = $('#p-preview-unknown'); if (b) b.textContent = r.system.unknown;
  } catch (e) { /* preview is best-effort */ }
}

async function pReset(key) {
  const rec = promptsState[key];
  if (!rec) return;
  if (!confirm('Reset this prompt to the built-in default?')) return;
  try {
    const r = await apiPost('/api/prompts/reset', { key });
    if (r.ok) { rec.input.value = rec.spec.default; rec.dirty = false; pRefreshPreview(); }
    else toast('Nothing to reset for ' + key, 'err');
  } catch (e) { toast('Reset failed: ' + e.message, 'err'); }
}

function pRevert() {
  for (const key of Object.keys(promptsState)) {
    const rec = promptsState[key];
    rec.input.value = rec.spec.text;   // last-loaded server value
    rec.dirty = false;
  }
  pRefreshPreview();
}

async function pSave() {
  const dirty = Object.keys(promptsState).filter(k => promptsState[k].dirty);
  if (!dirty.length) { toast('Nothing to save.'); return; }
  const values = {};
  for (const k of dirty) values[k] = promptsState[k].input.value;
  const btn = $('#p-save'); btn.disabled = true;
  try {
    const r = await apiPost('/api/prompts', { values });
    for (const k of Object.keys(values)) {
      const rec = promptsState[k];
      rec.spec.text = values[k];   // server now stores this
      rec.dirty = false;
    }
    toast('Saved — applies on the next interaction.', 'ok');
    pRefreshPreview();
  } catch (e) {
    toast('Save failed: ' + e.message, 'err');
  } finally { btn.disabled = false; }
}

// ---------------------------------------------------------------- export / import
// The export modal is shared: a single session (from a history row or the
// detail view) and "Export all" (from the history toolbar) both open it with
// their own `count` / per-part availability numbers. Import is a second mode
// of the same modal (file picker + overwrite).
function exportModal(opts) {
  // opts: { title, note, count, parts: {transcript, snapshot, clip} }
  // parts[key] === null means "availability not checked" (single-session
  // modal): the note column is omitted. A number means "N of count".
  const overlay = h('div', { class: 'overlay' });
  const body = [];
  for (const key of ['transcript', 'snapshot', 'clip']) {
    const avail = key === 'transcript' ? opts.count : opts.parts[key];
    const label = key === 'transcript' ? 'Transcript text' :
                  key === 'snapshot' ? 'Snapshot' : 'Video clip (Frigate)';
    const note = key === 'transcript' ? '' :
      (avail === null ? '' : avail + ' of ' + opts.count + ' available');
    body.push(h('label', { class: 'opt' },
      h('input', { type: 'checkbox', checked: 'true', 'data-part': key }),
      h('span', { text: label }),
      h('span', { class: 'n', text: note })));
  }
  if (opts.mode === 'import') body.length = 0;
  const box = h('div', { class: 'modal' },
    h('div', { class: 'modal-title', text: opts.mode === 'import' ? 'Import history' : opts.title }),
    h('div', { class: 'modal-note', text: opts.mode === 'import'
      ? 'Upload a zip previously exported from Doorman. Transcript is required; snapshot and clip are included if present.'
      : 'Only parts that exist for a session are included. Clips are fetched from Frigate at export time.' }),
    opts.mode === 'import'
      ? h('div', {},
          h('input', { type: 'file', accept: '.zip' }),
          h('label', { class: 'opt' },
            h('input', { type: 'checkbox' }),
            h('span', { text: 'Overwrite sessions that already exist' })))
      : h('div', { class: 'modal-opts' }, ...body),
    h('div', { class: 'modal-foot' },
      h('button', { class: 'btn ghost sm', onclick: () => overlay.remove(), text: 'Cancel' }),
      h('button', { class: 'btn sm', text: opts.mode === 'import' ? 'Import' : 'Export',
        onclick: () => opts.onConfirm(overlay) })));
  overlay.append(box);
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.body.append(overlay);
  return overlay;
}

let exportSelection = new Set();   // session ids checked on the history rows
const histRegistry = {};           // session_id -> row summary (for availability math)

// Shared: save a Response body as a named .zip; cleans up the object URL.
async function saveZip(res, filename) {
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  return blob;
}

// Which part checkboxes in the open modal are on? -> 'transcript,clip,snippet'
function checkedParts(overlay) {
  return $$('.opt input', overlay)
    .filter(i => i.checked)
    .map(i => i.dataset.part)
    .join(',');
}

// Per-part availability across the currently-checked rows (for the selected modal).
function selectedAvail() {
  let count = 0, snapshot = 0, clip = 0;
  for (const id of exportSelection) {
    const s = histRegistry[id];
    if (!s) continue;
    count++;
    if (s.has_snapshot) snapshot++;
    if (s.has_clip) clip++;
  }
  return { count, snapshot, clip };
}

// Keep the toolbar's "Export selected (N)" label + enabled state in sync.
function updateExportSelectedBtn() {
  const btn = $('#export-selected-btn');
  if (!btn) return;
  const n = exportSelection.size;
  btn.textContent = 'Export selected' + (n ? ' (' + n + ')' : '');
  btn.disabled = n === 0;
}

async function doExportSession(id) {
  // single session: open the modal, all three parts checked. No per-session
  // availability pre-probe (one extra request per row would be wasteful);
  // unavailable parts are simply absent from the zip and the toast says what
  // was requested.
  exportModal({
    mode: 'export',
    title: 'Export this session',
    count: 1,
    parts: { snapshot: null, clip: null },
    onConfirm: async (overlay) => {
      const parts = checkedParts(overlay);
      if (!parts) { toast('Pick at least one part.', 'err'); return; }
      overlay.remove();
      try {
        const res = await fetch('/api/sessions/' + encodeURIComponent(id) + '/export?parts=' + encodeURIComponent(parts));
        if (res.status === 409) { toast('This session is live right now; export it after it ends.', 'err'); return; }
        if (!res.ok) {
          let msg = 'HTTP ' + res.status;
          try { msg = (await res.json()).error || msg; } catch (e) {}
          throw new Error(msg);
        }
        await saveZip(res, 'doorman-' + id + '.zip');
        toast('Exported ' + parts + '.', 'ok');
      } catch (e) {
        toast('Export failed: ' + e.message, 'err');
      }
    },
  });
}

async function doExportBulk(title, available, ids) {
  // One zip of many sessions. available: {count, snapshot, clip}.
  // ids: array of session ids, or null/undefined for the whole history.
  exportModal({
    mode: 'export',
    title,
    count: available.count,
    parts: { snapshot: available.snapshot, clip: available.clip },
    onConfirm: async (overlay) => {
      const parts = checkedParts(overlay);
      if (!parts) { toast('Pick at least one part.', 'err'); return; }
      const q = new URLSearchParams({ parts, limit: '10000' });
      if (ids && ids.length) q.set('ids', ids.join(','));
      overlay.remove();
      const btn = ids ? $('#export-selected-btn') : $('#export-all-btn');
      if (btn) { btn.disabled = true; btn.textContent = 'Exporting…'; }
      try {
        const res = await fetch('/api/sessions/export-all?' + q.toString());
        if (!res.ok) {
          let msg = 'HTTP ' + res.status;
          try { msg = (await res.json()).error || msg; } catch (e) {}
          throw new Error(msg);
        }
        const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '');
        await saveZip(res, 'doorman-history' + (ids ? '-selected-' : '-') + stamp + '.zip');
        toast('Exported ' + available.count + ' session' + (available.count === 1 ? '' : 's') + ' (' + parts + ').', 'ok');
      } catch (e) {
        toast('Export failed: ' + e.message, 'err');
      } finally {
        if (btn) { btn.disabled = false; updateExportSelectedBtn(); }
      }
    },
  });
}

function exportAllFromToolbar() {
  // availability computed from a fresh full history fetch (has_snapshot/has_clip).
  // /api/history returns {sessions: [...], total: N} (pagination refactor).
  apiGet('/api/history?limit=all&offset=0')
    .then(d => {
      const all = d.sessions;
      if (!all.length) { toast('Nothing to export.', 'ok'); return; }
      doExportBulk('Export all history (' + all.length + ' sessions)',
        { count: all.length,
          snapshot: all.filter(x => x.has_snapshot).length,
          clip: all.filter(x => x.has_clip).length },
        null);
    })
    .catch(e => toast('History unavailable: ' + e.message, 'err'));
}

function exportSelectedFromToolbar() {
  if (!exportSelection.size) { toast('Check one or more rows first.', 'err'); return; }
  doExportBulk('Export selected sessions',
    selectedAvail(),
    Array.from(exportSelection));
}

async function doImport() {
  exportModal({
    mode: 'import',
    title: 'Import history',
    note: '',
    count: 0,
    parts: {},
    onConfirm: async (overlay) => {
      const input = $('input[type=file]', overlay);
      if (!input || !input.files || !input.files.length) { toast('Choose a .zip file first.', 'err'); return; }
      const overwrite = $('input[type=checkbox]', overlay).checked;
      const form = new FormData();
      form.append('file', input.files[0]);
      if (overwrite) form.append('overwrite', 'true');
      try {
        const res = await fetch('/api/sessions/import', { method: 'POST', body: form });
        const j = await res.json();
        if (res.status === 409) {
          toast('Already exists: ' + (j.conflicts || []).join(', ') + '. Re-open with "overwrite".', 'err');
          return;
        }
        if (!res.ok) throw new Error(j.error || 'HTTP ' + res.status);
        overlay.remove();
        let msg = 'Imported ' + j.imported.length + ' session' + (j.imported.length === 1 ? '' : 's') + '.';
        if (j.warnings && j.warnings.length) msg += ' (' + j.warnings.length + ' note' + (j.warnings.length === 1 ? '' : 's') + ')';
        toast(msg, 'ok');
        renderHistory();
      } catch (e) {
        toast('Import failed: ' + e.message, 'err');
      }
    },
  });
}

// ---------------------------------------------------------------- router
let timers = [];   // active setInterval ids for the current view
function stopTimers() { timers.forEach(clearInterval); timers = []; }

function route() {
  const hash = location.hash || '#dashboard';
  const m = hash.match(/^#session\/(.+)$/);
  const tab = m ? 'session' : hash.slice(1) || 'dashboard';
  // A session is viewed *under* whichever list it was opened from (the
  // dashboard's "last visit" card or the history tab). Track that so the
  // correct tab stays highlighted and Back returns to the right list.
  let parentTab;
  if (tab === 'session') {
    parentTab = sessionParent;
  } else {
    parentTab = tab;
    if (tab === 'dashboard' || tab === 'history') sessionParent = tab;
  }
  currentTab = tab;

  $$('#tabs a').forEach(a => a.classList.toggle('active', a.dataset.tab === parentTab || (tab === 'live' && a.dataset.tab === 'dashboard')));

  stopTimers();
  if (tab === 'dashboard') {
    renderDashboard();
    timers.push(setInterval(renderDashboard, 3000));
  } else if (tab === 'history') {
    renderHistory();
  } else if (tab === 'session') {
    renderSession(decodeURIComponent(m[1]));
  } else if (tab === 'live') {
    renderLive();
  } else if (tab === 'prompts') {
    renderPrompts();
  } else if (tab === 'settings') {
    renderSettings();
  } else {
    location.hash = '#dashboard';
    return;
  }
  window.scrollTo(0, 0);
}

window.addEventListener('hashchange', route);
document.addEventListener('DOMContentLoaded', () => {
  const footer = $('#footer');
  footer.innerHTML = 'Doorman<sup>DX</sup> web UI · trusted LAN, no auth';
  route();
});
