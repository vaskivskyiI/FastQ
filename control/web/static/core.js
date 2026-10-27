// FastQ dashboards - shared helpers, live PVs, ELOG composer (no build step, no libraries).
'use strict';

// append(a, cond ? b : null): conditional children are the norm here - skip null / false instead of printing them
for (const proto of [Element.prototype, DocumentFragment.prototype]) {
  for (const m of ['append', 'prepend']) {
    const orig = proto[m];
    proto[m] = function (...kids) { return orig.apply(this, kids.flat(3).filter(k => k != null && k !== false)); };
  }
}
const $ = (sel, el = document) => el.querySelector(sel);
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'class') e.className = v;
    else if (k === 'html') e.innerHTML = v;
    else if (k === 'value') e.value = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat(3)) if (k != null && k !== false) e.append(k instanceof Node ? k : String(k));
  return e;
};
function toast(msg, err = false) {
  const d = h('div', {class: err ? 'err' : ''}, msg); $('#toast').append(d);
  setTimeout(() => d.remove(), err ? 9000 : 3500);
}
async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  let body = null;
  try { body = await r.json(); } catch (e) { /* not json */ }
  if (r.status === 401 && url !== '/api/login' && !api.loggingIn) { api.loggingIn = true; loginScreen('Your session ended - log in again.'); }
  if (!r.ok) throw new Error((body && body.detail) || `${r.status} ${r.statusText}`);
  return body;
}
// accounts (fastq.users): the whole page waits behind this until a user logged in
function loginScreen(msg = '') {
  const user = h('input', {placeholder: 'user', autocomplete: 'username'});
  const pw = h('input', {type: 'password', placeholder: 'password', autocomplete: 'current-password'});
  const err = h('p', {class: 'small bad-text'}, msg);
  const go = async () => {
    try { await post('/api/login', {user: user.value.trim(), password: pw.value}); location.reload(); }
    catch (e) { err.textContent = e.message; pw.select(); }
  };
  document.body.innerHTML = '';
  document.body.append(h('div', {class: 'login'}, h('div', {class: 'panel enter-scope'}, h('h2', {}, 'FastQ'),
    user, pw, h('button', {class: 'primary', onclick: go}, 'Log in'), err)));
  setTimeout(() => user.focus(), 50);
}
const can = need => !S.me || ['viewer', 'operator', 'admin'].indexOf(S.me.role) >= ['viewer', 'operator', 'admin'].indexOf(need);
const jsonReq = method => (url, body) => api(url, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body ?? {})});
const post = jsonReq('POST'), put = jsonReq('PUT'), del = url => api(url, {method: 'DELETE'});
async function tryApi(fn, what) { try { return await fn(); } catch (e) { toast(`${what}: ${e.message}`, true); return null; } }

function fmt(v, prec, units) {
  if (v == null) return '—';
  if (Array.isArray(v)) return `[${v.length}]`;
  if (typeof v === 'number') {
    let s;
    if (prec != null && prec >= 0 && Math.abs(v) < 1e7 && (Math.abs(v) >= 1e-4 || v === 0)) s = v.toFixed(Math.min(prec, 12));
    else if (Number.isInteger(v)) s = String(v);
    else s = Math.abs(v) >= 1e5 || Math.abs(v) < 1e-3 ? v.toExponential(4) : (+v.toPrecision(7)).toString();
    return units ? `${s} ${units}` : s;
  }
  return units ? `${v} ${units}` : String(v);
}
const fmtTime = t => t ? new Date(t * 1000).toLocaleString() : '—';
const fmtShort = t => { if (!t) return '—'; const d = new Date(t * 1000), n = new Date(); return d.toDateString() === n.toDateString() ? d.toLocaleTimeString() : d.toLocaleString(); };
const fmtDur = s => s == null ? '—' : s < 120 ? `${s.toFixed(0)} s` : s < 7200 ? `${(s / 60).toFixed(1)} min` : s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${(s / 86400).toFixed(1)} d`;
const truthy = v => v === 1 || v === true || (typeof v === 'string' && /^(on|1|true|yes|open)$/i.test(v));
function parseValue(raw) {
  const t = String(raw).trim();
  if (t === '') return undefined;
  if (/^(True|False)$/.test(t)) return t === 'True';
  if (t === 'None') return null;
  try { return JSON.parse(t); } catch (e) { /* not json */ }
  try { return JSON.parse(t.replace(/'/g, '"').replace(/\bTrue\b/g, 'true').replace(/\bFalse\b/g, 'false').replace(/\bNone\b/g, 'null')); } catch (e) { /* python-ish */ }
  return t;
}
const store = {
  get(k, d) { try { const v = localStorage.getItem('fastq.' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('fastq.' + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};
const statusPill = st => h('span', {class: 'pill ' + (!st ? 'warn' : st === 'success' || st === 'completed' ? 'ok' : st === 'running' ? 'warn' : 'bad')}, st || 'running');

// ------------------------------------------------------------------ modal dialog
function modal(title, body, actions = []) {
  const close = () => back.remove();
  const back = h('div', {class: 'modal-back', onclick: e => { if (e.target === back) close(); }},
    h('div', {class: 'modal'}, h('div', {class: 'modal-head'}, h('b', {}, title), h('button', {class: 'ghost', onclick: close}, '✕')),
      body, actions.length ? h('div', {class: 'row end'}, actions.map(([label, fn, cls]) => h('button', {class: cls || '', onclick: async () => { if (await fn() !== false) close(); }}, label))) : null));
  document.body.append(back);
  return close;
}

// ------------------------------------------------------------------ live PVs (websocket)
const PV = {
  ws: null, cbs: new Map(), last: new Map(), open: false,
  connect() {
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/pv');
    this.ws = ws;
    ws.onopen = () => { this.open = true; if (this.cbs.size) ws.send(JSON.stringify({sub: [...this.cbs.keys()]})); };
    ws.onmessage = ev => {
      for (const p of JSON.parse(ev.data)) {
        this.last.set(p.name, p);
        for (const cb of this.cbs.get(p.name) || []) cb(p);
      }
    };
    ws.onclose = () => {
      this.open = false;
      for (const [name, cbs] of this.cbs) for (const cb of cbs) cb({...(this.last.get(name) || {name}), connected: false});
      setTimeout(() => this.connect(), 2000);
    };
  },
  sub(names, cb) {
    const fresh = [];
    for (const n of names) {
      if (!this.cbs.has(n)) { this.cbs.set(n, new Set()); fresh.push(n); }
      this.cbs.get(n).add(cb);
      if (this.last.has(n)) cb(this.last.get(n));
    }
    if (fresh.length && this.open) this.ws.send(JSON.stringify({sub: fresh}));
    return () => {
      const gone = [];
      for (const n of names) { const s = this.cbs.get(n); if (s) { s.delete(cb); if (!s.size) { this.cbs.delete(n); gone.push(n); } } }
      if (gone.length && this.open) this.ws.send(JSON.stringify({unsub: gone}));
    };
  },
};
function pvCell(name, opts = {}) {
  const el = h('span', {class: 'v', title: name}, '…');
  el._unsub = PV.sub([name], p => {
    if (!p.connected) { el.textContent = 'offline'; el.className = 'v muted'; return; }
    el.className = 'v';
    el.textContent = opts.format ? opts.format(p.value, p) : fmt(p.value, opts.precision ?? p.precision, opts.units ?? p.units);
    if (opts.onValue) opts.onValue(p.value, p);
  });
  return el;
}
function cleanupPVs(root) { root.querySelectorAll('.v').forEach(e => e._unsub && e._unsub()); }
// A PV's values over the last windowS: the server's recorded history (fastq.history) + live updates,
// so a trend keeps its past across page changes and restarts. Long windows come back as min/max envelopes.
class Trend {
  constructor(name, label, windowS = 1800) { Object.assign(this, {name, label, windowS, x: [], y: [], lo: null, hi: null, listeners: [], gen: 0}); }
  start() {
    this.load();
    this.un = PV.sub([this.name], p => {
      if (!p.connected || typeof p.value !== 'number' && typeof p.value !== 'boolean') return;
      const t = p.ts || Date.now() / 1000;
      if (this.x.length && t <= this.x[this.x.length - 1]) return;
      this.x.push(t); this.y.push(+p.value);
      if (this.lo) { this.lo.push(+p.value); this.hi.push(+p.value); }
      let cut = 0; while (cut < this.x.length && this.x[cut] < t - this.windowS) cut++;
      if (cut) { this.x.splice(0, cut); this.y.splice(0, cut); if (this.lo) { this.lo.splice(0, cut); this.hi.splice(0, cut); } }
      this.emit();
    });
    return this;
  }
  async load() {
    const gen = ++this.gen, now = Date.now() / 1000;
    const h = await api(`/api/history?pv=${encodeURIComponent(this.name)}&start=${now - this.windowS}&end=${now}&points=1500`).catch(() => null);
    if (!h || gen !== this.gen) return;
    const live = this.x.map((t, i) => [t, this.y[i]]).filter(([t]) => t > (h.t[h.t.length - 1] || 0));
    this.x = h.t.concat(live.map(a => a[0])); this.y = h.v.concat(live.map(a => a[1]));
    this.lo = h.vmin ? h.vmin.concat(live.map(a => a[1])) : null; this.hi = h.vmax ? h.vmax.concat(live.map(a => a[1])) : null;
    this.source = h.source; this.emit();
  }
  setWindow(s) { this.windowS = s; this.load(); }
  emit() { this.listeners.forEach(f => f()); }
  stop() { this.gen++; this.un && this.un(); }
}

// ------------------------------------------------------------------ shared state
const S = {lab: null, devices: [], profiles: {}, schemas: null, status: null, live: null, pendingAttachments: []};
async function loadDevices() {
  const d = await tryApi(() => api('/api/devices'), 'device directory');
  if (d) { S.devices = d.devices; S.directories = d.directories; S.dirErrors = d.errors; }
  return S.devices;
}
async function loadSchemas(force = false) {
  if (!S.schemas || force) S.schemas = await api('/api/schemas').catch(() => ({}));
  return S.schemas;
}
// what a device is comes from its schema's ophyd hints (devkit Instrument.ophyd), never from param names
const ophydOf = name => ((S.schemas || {})[name] || {}).ophyd || {};
const isCamera = name => ophydOf(name).detector === 'camera';
const roleDevice = role => S.devices.find(d => d.name === (S.lab.roles || {})[role]);
const rolesOf = name => Object.entries(S.lab.roles || {}).filter(([, n]) => n === name).map(([r]) => r);
// Write a control. safety (from the IOC schema, enforced again by the server):
//   safe - at once; confirm - a dialog (Enter = write); protected - the lab PIN.
async function putPV(pv, value, label, safety = 'confirm') {
  const send = async pin => {
    let r;
    try {
      r = await post('/api/pv/put', {pv, value, pin});
    } catch (e) {
      if (!pin && /PIN required/.test(String(e.message || e))) return 'pin';  // the server knows better: ask for it
      toast(`write: ${e.message || e}`, 'bad');
      return false;
    }
    toast(r && r.started ? `${label || pv}: started (follow Busy / LastError)` : `${label || pv} = ${value}`);
    return true;
  };
  if (safety === 'safe') { const r = await send(); return r === 'pin' ? putPV(pv, value, label, 'protected') : r; }
  return new Promise(resolve => {
    let done = false;
    const finish = v => { if (!done) { done = true; resolve(v); } };
    const accounts = S.accounts;  // with accounts: the user's own password (the audit log names who)
    const pin = safety === 'protected' ? h('input', {type: 'password', inputmode: accounts ? null : 'numeric', autocomplete: 'off',
      placeholder: accounts ? 'your password' : 'PIN', style: 'width:160px'}) : null;
    const body = h('div', {class: 'enter-scope'}, h('p', {}, label || pv), h('p', {class: 'mono small muted'}, `${pv} = ${JSON.stringify(value)}`),
      pin ? h('p', {class: 'small'}, accounts ? `🔒 protected control - confirm with your password (${S.me.name})` : '🔒 protected control - enter the lab PIN') : null, pin);
    const close = modal(safety === 'protected' ? 'Protected control' : 'Write to the instrument?', body,
      [[safety === 'protected' ? 'Unlock & write' : 'Write', async () => { const ok = await send(pin ? pin.value : undefined); if (ok === 'pin') { finish(putPV(pv, value, label, 'protected')); return true; } if (ok) finish(true); else if (pin) pin.select(); return ok; }, safety === 'protected' ? 'danger' : 'primary'],
       ['Cancel', () => finish(false), 'ghost']]);
    const back = document.querySelector('.modal-back:last-of-type');
    if (back) new MutationObserver((m, obs) => { if (!document.body.contains(back)) { obs.disconnect(); finish(false); } }).observe(document.body, {childList: true});
    setTimeout(() => (pin || back && back.querySelector('.modal button.primary, .modal button.danger') || {focus() {}}).focus(), 30);
    void close;
  });
}
const SAFETY_BADGE = {safe: '', confirm: '⚠', protected: '🔒'};
// effective level: the lab's override (lab.yaml security.overrides "device.PV"), else the IOC's
const controlSafety = (dev, suffix, declared) => ((S.lab.security || {}).overrides || {})[`${dev.name}.${suffix}`]
  || declared || (((S.profiles[dev.type] || {}).controls || []).find(c => c.write_suffix === suffix) || {}).safety || 'confirm';
function download(name, text, type = 'text/plain') {
  const a = h('a', {href: URL.createObjectURL(new Blob([text], {type})), download: name.replace(/[^\w.-]+/g, '_')});
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// Inputs: focusing selects the value (type to replace, not append); Enter presses the field's button
// (the first button of its .ctl / .enter-scope, or the dialog's main button).
document.addEventListener('focusin', e => {
  const t = e.target;
  if (t.tagName === 'INPUT' && /^(text|number|search|password|)$/.test(t.type || '') && t.select) setTimeout(() => { try { t.select(); } catch (err) { /* type without selection */ } }, 0);
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Enter' || e.target.tagName !== 'INPUT' || e.isComposing) return;
  const scope = e.target.closest('.ctl, .enter-scope, .modal');
  const btn = scope && (e.target.closest('.modal') ? e.target.closest('.modal').querySelector('.row.end button:not(.ghost)') : scope.querySelector('button'));
  if (btn) { e.preventDefault(); btn.click(); }
});

// ------------------------------------------------------------------ ELOG
function screenCaptureAvailable() { return window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia; }
async function captureScreen() {
  const stream = await navigator.mediaDevices.getDisplayMedia({video: true, audio: false});
  try {
    const video = h('video', {muted: true, playsinline: true});
    video.srcObject = stream; await video.play();
    await new Promise(r => setTimeout(r, 350));
    const c = h('canvas'); c.width = video.videoWidth; c.height = video.videoHeight;
    c.getContext('2d').drawImage(video, 0, 0);
    return await new Promise(res => c.toBlob(res, 'image/png'));
  } finally { stream.getTracks().forEach(t => t.stop()); }
}
const stamp = () => new Date().toISOString().slice(0, 19).replace(/[-:]/g, '').replace('T', '_');
let _logbooks = null;
async function elogLogbooks() {
  if (!_logbooks) _logbooks = api('/api/elog/logbooks').catch(e => {  // tried again next time; said once
    if (!S.elogWarned) toast('ELOG: ' + e.message, true);
    S.elogWarned = true; _logbooks = null; return [];
  });
  return _logbooks;
}
async function fillLogbookSelect(sel, current, placeholder) {
  const books = await elogLogbooks();
  sel.innerHTML = '';
  if (placeholder) sel.append(h('option', {value: ''}, placeholder));
  if (current && !books.some(b => b.name === current)) sel.append(h('option', {value: current}, current));
  const groups = {};
  books.forEach(b => (groups[b.group || 'Logbooks'] = groups[b.group || 'Logbooks'] || []).push(b));
  for (const [g, bs] of Object.entries(groups))
    sel.append(h('optgroup', {label: g}, bs.map(b => h('option', {value: b.name, title: b.comment}, b.name + (b.comment ? ` — ${b.comment.slice(0, 50)}` : '')))));
  if (current) sel.value = current;
}

// composer: a new ELOG entry (text, screenshots, files), optionally a reply in a run's thread
function composer(opts = {}) {
  const el = h('div', {class: 'form composer'});
  const files = [];
  const lab = S.lab.elog;
  if (!lab.enabled) { el.append(h('p', {class: 'muted'}, 'ELOG is not configured (lab.yaml: elog.url).')); return el; }
  const bookSel = h('select', {}, lab.current ? h('option', {value: lab.current}, lab.current) : null);
  const typeSel = h('select', {title: 'Category'});
  const catName = (lab.attributes || {}).category || 'Category';
  async function loadCategories() {
    const book = bookSel.value; if (!book) return;
    const o = await api(`/api/elog/options?logbook=${encodeURIComponent(book)}`).catch(() => ({}));
    const cats = o[catName] || ['Comment', 'Info', 'Problem', 'Result'];
    const keep = typeSel.value || store.get('category', 'Comment');
    typeSel.innerHTML = ''; cats.forEach(c => typeSel.append(h('option', {}, c)));
    typeSel.value = cats.includes(keep) ? keep : (cats.includes('Comment') ? 'Comment' : cats[0]);
  }
  fillLogbookSelect(bookSel, lab.current).then(loadCategories);
  bookSel.addEventListener('change', loadCategories);
  typeSel.addEventListener('change', () => store.set('category', typeSel.value));
  const runSel = h('select', {}, h('option', {value: ''}, 'no run'));
  const author = h('input', {value: store.get('author', ''), placeholder: 'your name', style: 'max-width:160px'});
  const subject = h('input', {placeholder: 'Subject'});
  const text = h('textarea', {rows: opts.rows || 5, placeholder: 'Text…  (Ctrl+V pastes a screenshot)'});
  const atts = h('div', {class: 'atts'});
  const fileIn = h('input', {type: 'file', multiple: true, style: 'display:none', onchange: () => { [...fileIn.files].forEach(f => add(f)); fileIn.value = ''; }});
  function add(blob, name) {
    const f = blob instanceof File ? blob : new File([blob], name || `screenshot_${stamp()}.png`, {type: blob.type || 'image/png'});
    files.push(f);
    const url = URL.createObjectURL(f);
    const box = h('div', {class: 'att'}, f.type.startsWith('image/') ? h('img', {src: url}) : null, h('div', {class: 'name'}, f.name),
      h('button', {class: 'x', onclick: () => { files.splice(files.indexOf(f), 1); box.remove(); URL.revokeObjectURL(url); }}, '×'));
    atts.append(box);
  }
  el._add = add;
  for (const a of S.pendingAttachments.splice(0)) add(a.blob, a.name);
  (async () => {
    const seen = new Set();
    const opt = (uid, label) => { if (!uid || seen.has(uid)) return; seen.add(uid); runSel.append(h('option', {value: uid}, label)); };
    const live = S.live && !S.live.exit_status ? S.live : null;
    if (live) opt(live.uid, `▶ running: ${live.scan_id} ${live.plan_name}`);
    (await api('/api/runs?limit=25').catch(() => [])).forEach(r => opt(r.uid, `Run ${r.scan_id ?? ''}: ${r.plan_name}${r.comment ? ' – ' + r.comment.slice(0, 40) : ''}`));
    const want = opts.runUid || (live && live.uid) || '';
    if (want && !seen.has(want)) opt(want, want.slice(0, 8));
    runSel.value = want;
  })();
  const send = h('button', {class: 'primary', onclick: async () => {
    if (!subject.value.trim() && !text.value.trim()) { toast('write a subject or text', true); return; }
    const fd = new FormData();
    fd.append('subject', subject.value.trim() || text.value.trim().split('\n')[0].slice(0, 80));
    fd.append('text', text.value); fd.append('logbook', bookSel.value); fd.append('run_uid', runSel.value);
    fd.append('category', typeSel.value); fd.append('author', author.value.trim());
    files.forEach(f => fd.append('files', f, f.name));
    store.set('author', author.value.trim());
    send.disabled = true;
    try {
      await api('/api/elog/entry', {method: 'POST', body: fd});
      toast(runSel.value ? 'posted as a reply to the run entry' : 'posted to ELOG');
      subject.value = ''; text.value = ''; files.splice(0); atts.innerHTML = '';
      opts.onPosted && opts.onPosted();
    } catch (e) { toast('ELOG: ' + e.message, true); }
    send.disabled = false;
  }}, 'Post');
  el._paste = e => {
    const items = [...((e.clipboardData || {}).items || [])].filter(i => i.kind === 'file');
    if (!items.length) return;
    e.preventDefault(); items.forEach(i => add(i.getAsFile(), i.type.startsWith('image/') ? `screenshot_${stamp()}.png` : undefined));
  };
  document.addEventListener('paste', el._paste);
  el.addEventListener('dragover', e => e.preventDefault());
  el.addEventListener('drop', e => { e.preventDefault(); [...e.dataTransfer.files].forEach(f => add(f)); });
  el.append(h('div', {class: 'row'}, runSel, bookSel, typeSel), subject, text, atts,
    h('div', {class: 'row'},
      h('button', {class: 'ghost', disabled: !screenCaptureAvailable(), title: 'capture a screen / window (needs localhost or https)',
        onclick: async () => { try { add(await captureScreen()); } catch (e) { if (e.name !== 'NotAllowedError') toast('capture: ' + e.message, true); } }}, '📷 Screen'),
      h('button', {class: 'ghost', onclick: () => fileIn.click()}, '📎 Files'), fileIn, h('span', {class: 'grow'}), author, send));
  return el;
}
function destroyComposers(root) { root.querySelectorAll('.composer').forEach(f => f._paste && document.removeEventListener('paste', f._paste)); }

// current experiment (sample + notebook), edited in a dialog from the top bar
function editExperiment() {
  const ex = S.lab.experiment || {};
  const sample = h('input', {value: ex.sample || '', placeholder: 'sample name'});
  const nb = h('select');
  if (S.lab.elog.enabled) fillLogbookSelect(nb, ex.notebook); else nb.disabled = true;
  modal('Experiment', h('div', {class: 'form'},
    h('label', {}, 'Sample - recorded in every new run and ELOG entry'), sample,
    h('label', {}, 'ELOG notebook of the experiment (run entries and comments)'), nb,
    h('p', {class: 'muted small'}, 'Applies from the next run; a running run keeps its values.')),
  [['Cancel', () => true, 'ghost'], ['Save', async () => {
    const r = await tryApi(() => post('/api/experiment', {sample: sample.value.trim(), notebook: nb.value}), 'experiment');
    if (!r) return false;
    S.lab.experiment = r; topbar(); toast('experiment updated');
  }, 'primary']]);
}

// ------------------------------------------------------------------ top bar: setup, experiment, queue / run state
function topbar() {
  const el = $('#topstatus'); if (!el || !S.lab) return;
  el.innerHTML = '';
  const s = S.status, live = S.live;
  const running = live && live.uid && !live.exit_status;
  el.append(
    h('button', {class: 'chip', title: 'sample and ELOG notebook of new runs', onclick: editExperiment},
      '🧪 ', (S.lab.experiment || {}).sample || 'no sample', S.lab.elog.enabled ? ['  ·  📓 ', (S.lab.experiment || {}).notebook || '—'] : null),
    running ? h('a', {class: 'chip run', href: '#live'}, '▶ ', `Run ${live.scan_id} ${live.plan_name}`,
      live.num_points ? ` ${live.events}/${live.num_points}` : ` ${live.events}`) : null,
    h('span', {class: 'chip ' + (!s ? 'bad' : s.manager_state === 'idle' ? '' : 'warn')},
      !s ? 'queue server offline' : `queue ${s.manager_state}${s.items_in_queue ? ' · ' + s.items_in_queue + ' waiting' : ''}`),
    S.alarmsActive ? h('a', {class: 'chip bad', href: '#system', title: 'active alarms (System page)'}, `⚠ ${S.alarmsActive} alarm${S.alarmsActive > 1 ? 's' : ''}`) : null,
    S.accounts && S.me ? h('button', {class: 'chip', title: `${S.me.role} - click to log out`, onclick: async () => {
      if (confirm(`Log out ${S.me.name}?`)) { await post('/api/logout'); location.reload(); }
    }}, '👤 ', S.me.display, h('span', {class: 'muted small'}, ` ${S.me.role}`)) : null,
    window.Notification ? h('button', {class: 'chip', title: 'desktop notification when a run ends or is held', onclick: async () => {
      const on = !store.get('notify', false);
      if (on && Notification.permission !== 'granted' && await Notification.requestPermission() !== 'granted') { toast('notifications blocked by the browser', true); return; }
      store.set('notify', on); topbar();
    }}, store.get('notify', false) ? '🔔' : '🔕') : null);
}
// a run ended / was held: toast, and a desktop notification if the user switched them on (🔔)
function announce(prev, cur) {
  if (!prev || !cur || !cur.uid) return;
  let msg = null;
  if (prev.uid === cur.uid && !prev.exit_status && cur.exit_status) msg = `${runTitle(cur)} finished: ${cur.exit_status}`;
  else if (prev.uid === cur.uid && !prev.suspended && cur.suspended && !cur.exit_status) msg = `${runTitle(cur)} held: ${cur.suspended}`;
  if (!msg) return;
  toast(msg, cur.exit_status && cur.exit_status !== 'success');
  if (store.get('notify', false) && window.Notification && Notification.permission === 'granted') new Notification('FastQ', {body: msg + (cur.comment ? `
${cur.comment}` : '')});
}
const runTitle = r => `Run ${r.scan_id ?? '?'} · ${r.plan_name || ''}`;
async function poll() {
  try { S.status = await api('/api/queue/status'); } catch (e) { S.status = null; }
  const prev = S.live;
  try { S.live = await api('/api/live'); } catch (e) { /* data dir not shared */ }
  announce(prev, S.live);
  try {  // alarm rules (fastq.alarms): new events as toasts / desktop notifications
    const a = await api(`/api/alarms?since=${S.alarmSince || Date.now() / 1000}`);
    S.alarmsActive = a.active;
    for (const e of a.events) {
      S.alarmSince = Math.max(S.alarmSince || 0, e.t);
      if (!e.notify) continue;
      toast(e.text.split('\n')[0], e.kind === 'active');
      if (store.get('notify', false) && window.Notification && Notification.permission === 'granted') new Notification('FastQ alarm', {body: e.text});
    }
    if (!S.alarmSince) S.alarmSince = Date.now() / 1000;
  } catch (e) { /* older server */ }
  topbar();
  document.dispatchEvent(new CustomEvent('fastq:poll'));
}
