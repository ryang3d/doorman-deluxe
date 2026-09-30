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

function fmtWhen(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  const now = Date.now();
  const diff = now - d.getTime();
  if (diff < 60e3) return 'just now';
  if (diff < 3600e3) return Math.floor(diff / 60e3) + 'm ago';
  const sameDay = d.toDateString() === new Date().toDateString();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (sameDay) return time;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
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
  const label = who + (m.kind ? ' · ' + m.kind : '') + ' · ' + (m.ts ? new Date(m.ts).toLocaleTimeString() : '');
  return h('div', { class: 'msg ' + role },
    h('div', { class: 'who', text: label }),
    h('div', { class: 'bubble', text: escText(m.text) }),
  );
}

function summaryLine(sum) {
  if (!sum) return 'No visits yet';
  const who = sum.recognized_name || 'Unknown';
  return who + ' · ' + sum.trigger + ' · ' + fmtWhen(sum.started_at) +
    (sum.duration_s != null ? ' · ' + fmtDur(sum.duration_s) : '') +
    ' · ' + sum.message_count + ' msg';
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
            h('div', { class: 'when', text: fmtWhen(s.last_visit.started_at) }),
            h('div', { class: 'name' },
              s.last_visit.recognized_name || h('span', { class: 'tag', text: 'Unknown' }),
              ' ', h('span', { class: 'tag', text: '· ' + s.last_visit.trigger })),
            h('div', { class: 'meta', text: summaryLine(s.last_visit).split(' · ').slice(1).join(' · ') }),
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
let histState = { offset: 0, limit: 30 };

function histItem(sum) {
  const who = sum.recognized_name || 'Unknown';
  return h('a', { href: '#session/' + sum.session_id, class: 'hist-item' },
    h('div', { class: 'when', text: fmtWhen(sum.started_at) }),
    h('div', { class: 'name' },
      who,
      h('span', { class: 'tag', text: ' · ' + sum.trigger })),
    h('div', { class: 'meta', text:
      (sum.duration_s != null ? fmtDur(sum.duration_s) + ' · ' : '') + sum.message_count + ' msg' }),
    sum.has_snapshot
      ? h('img', { class: 'snap', src: '/api/snapshot/' + sum.session_id, alt: 'snapshot',
                  loading: 'lazy',
                  onerror() { this.replaceWith(h('span', { class: 'snap', style: 'display:block' })); } })
      : null,
  );
}

async function loadHistory(reset) {
  if (reset) histState = { offset: 0, limit: 30 };
  try {
    const items = await apiGet('/api/history?limit=' + histState.limit + '&offset=' + histState.offset);
    if (reset) view.replaceChildren();
    const list = $('.hist-list', view) || view;
    if (!items.length && reset) {
      view.append(h('div', { class: 'hist-empty', text: 'No visits recorded yet.' }));
      return;
    }
    for (const sum of items) list.append(histItem(sum));
    histState.offset += items.length;
    const btn = $('.load-more', view);
    if (items.length < histState.limit) {
      if (btn) btn.remove();
    } else if (!btn) {
      view.append(h('button', { class: 'load-more', onclick: () => loadHistory(false) }, 'Load more'));
    }
  } catch (e) {
    view.append(h('div', { class: 'muted-line', text: 'History unavailable: ' + e.message }));
  }
}

function renderHistory() {
  view.replaceChildren(h('div', { class: 'section-title', text: 'Visit history' }), h('div', { class: 'hist-list' }));
  loadHistory(true);
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
  const when = s.started_at ? new Date(s.started_at).toLocaleString() : '';
  view.replaceChildren(
    h('div', { class: 'detail' },
      h('div', { class: 'head' },
        h('button', { class: 'back', onclick: goBack }, '←'),
        h('h2', { text: s.recognized_name || 'Unknown' }),
        h('span', { class: 'meta', text:
          when + ' · ' + s.trigger +
          (s.status ? ' · ' + s.status : '') +
          (s.duration_s != null ? ' · ' + fmtDur(s.duration_s) : '') +
          (s.doorbell ? ' · doorbell' : '') +
          (s.label ? ' · ' + s.label : '') })),
      h('div', { class: 'body' },
        (s.messages && s.messages.length)
          ? h('div', { class: 'transcript' }, s.messages.map(msgNode))
          : h('div', { class: 'transcript' }, h('div', { class: 'muted-line', text: 'No transcript.' })),
        h('div', { class: 'snap-pane' },
          s.snapshot
            ? h('img', { src: '/api/snapshot/' + s.session_id, alt: 'Snapshot',
                        onerror() { this.replaceWith(h('div', { class: 'no-snap', text: 'Snapshot unavailable' })); } })
            : h('div', { class: 'no-snap', text: 'No snapshot' })
        )
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

function buildSettings() {
  const fields = settingsCache ? Object.values(settingsCache) : [];
  const root = h('div', {});
  const bar = h('div', { class: 'settings-bar' },
    h('span', { class: 'hint', id: 'dirty-hint', text: 'Changes apply live unless marked “restart”.' }),
    h('button', { class: 'btn ghost', id: 'revert-btn', onclick: revertSettings }, 'Revert'),
    h('button', { class: 'btn ghost', id: 'test-notify-btn', onclick: sendTestNotification }, 'Test notification'),
    h('button', { class: 'btn', id: 'save-btn', onclick: saveSettings }, 'Save'),
  );
  root.append(bar);

  const groups = {};
  for (const spec of fields) (groups[spec.group] = groups[spec.group] || []).push(spec);

  for (const [gname, specs] of Object.entries(groups)) {
    const dl = h('details', { class: 'group' },
      h('summary', { text: gname }),
      h('div', { class: 'rows' }));
    const rows = $('.rows', dl);
    for (const spec of specs) rows.append(settingRow(spec));
    root.append(dl);
  }
  view.replaceChildren(root);
  updateDirtyHint();
}

function settingRow(spec) {
  const wrap = h('div', {});
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

// ---------------------------------------------------------------- router
let timers = [];   // active setInterval ids for the current view
function stopTimers() { timers.forEach(clearInterval); timers = []; }

function route() {
  const hash = location.hash || '#dashboard';
  const m = hash.match(/^#session\/(.+)$/);
  const tab = m ? 'session' : hash.slice(1) || 'dashboard';
  const parentTab = tab === 'session' ? 'dashboard' : tab;   // session belongs under the dashboard tab
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
  footer.textContent = 'Doorman web UI · trusted LAN, no auth';
  route();
});
