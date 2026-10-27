// FastQ pages: Control room, Experiments (plans, scan builder, queue), Runs, Devices, Logbook, System.
'use strict';

const Pages = {};

// queue server actions; ending a run (re_stop / abort) goes through /api/run/{stop|abort}, which pauses it first
async function runAction(action) {
  const url = action === 're_stop' ? '/api/run/stop' : action === 'abort' ? '/api/run/abort'
    : `/api/queue/${action === 'pause_now' ? 'pause' : action}`;
  const r = await tryApi(() => post(url, action === 'pause_now' ? {option: 'immediate'} : {}), action);
  if (r) toast(`${action.replace('_', ' ')} ✓`);
  poll();
}

// ================================================================== Control room
Pages.live = async root => {
  await Promise.all([loadDevices(), loadSchemas()]);
  const cleanups = [];
  const banner = h('div', {class: 'banner'});
  let uid = null, detail = null, plan = null;
  const grid = new PlotGrid({uid: () => uid, plan: () => plan, detail: () => detail, title: () => detail ? `run${detail.scan_id}` : 'run'});
  cleanups.push(() => grid.destroy());
  const side = h('div', {class: 'side'});
  const camBox = h('div', {class: 'plot-grid'});
  const showCams = h('input', {type: 'checkbox', checked: store.get('liveCams', false)});
  root.append(banner, h('div', {class: 'live-layout'}, h('div', {}, grid.root, camBox), side));

  // side: the devices of the lab roles, live
  const roles = Object.entries(S.lab.roles || {});
  const vals = h('table', {class: 'compact'});
  for (const [role, name] of roles) {
    const dev = S.devices.find(d => d.name === name);
    if (!dev || !dev.enabled) continue;
    const prof = S.profiles[dev.type] || {};
    const key = (prof.overview || []).find(k => !/Connected|IOC:/.test(k)) || (prof.fields || []).map(f => f.suffix).find(k => !/Connected|IOC:/.test(k));
    const f = (prof.fields || []).find(x => x.suffix === key) || {};
    vals.append(h('tr', {}, h('td', {}, h('a', {href: `#devices?d=${name}`}, role), h('div', {class: 'small muted'}, name)),
      h('td', {}, key ? pvCell(dev.prefix + key, {precision: f.precision}) : ''), h('td', {}, pvCell(dev.prefix + 'Connected', {format: v => truthy(v) ? '●' : '○'}))));
  }
  const down = (S.dirErrors || []).map(d => h('div', {class: 'small bad-text'}, `no devices: directory ${d} not reachable (IOC host not running?)`));
  side.append(h('div', {class: 'panel'}, h('div', {class: 'panel-head'}, h('b', {}, 'Lab'), h('span', {class: 'grow'}),
    h('label', {class: 'small'}, showCams, ' cameras')), ...down, vals),
  h('div', {class: 'panel'}, h('div', {class: 'panel-head'}, h('b', {}, 'Quick note')), composer({rows: 3})));
  const drawCams = () => {
    camBox.innerHTML = '';
    if (!showCams.checked) return;
    S.devices.filter(d => d.enabled && isCamera(d.name))
      .forEach(d => camBox.append(cameraPanel(d, cleanups)));
  };
  showCams.addEventListener('change', () => { store.set('liveCams', showCams.checked); drawCams(); });
  drawCams();

  const drawBanner = () => {
    const L = S.live || {}, s = S.status || {};
    banner.innerHTML = '';
    if (!L.uid) { banner.append(h('div', {class: 'muted'}, 'No run yet. Start one on the Experiments page.')); return; }
    const running = !L.exit_status;
    const frac = L.num_points ? Math.min(1, L.events / L.num_points) : null;
    const motors = new Set([...(L.motors || []), 'repeat']);
    const pos = Object.entries(L.position || {}).filter(([k]) => motors.has(k) || [...motors].some(m => k === m || k.startsWith(m + '_'))).slice(0, 8);
    banner.append(
      h('div', {class: 'b-main'},
        h('div', {class: 'b-title'}, statusPill(running ? (s.re_state === 'paused' ? 'paused' : 'running') : L.exit_status), ' ',
          h('a', {href: `#runs?uid=${L.uid}`}, runTitle(L)), L.comment ? h('span', {class: 'muted'}, ' — ' + L.comment) : null),
        h('div', {class: 'progress'}, h('div', {style: `width:${frac == null ? (running ? 100 : 0) : frac * 100}%`, class: frac == null && running ? 'indet' : ''})),
        h('div', {class: 'small muted'}, `${L.events || 0}${L.num_points ? ' / ' + L.num_points : ''} points · ${fmtDur(L.elapsed_s)}`,
          running && L.eta_s != null ? ` · ${fmtDur(L.eta_s)} left (≈ ${new Date(Date.now() + L.eta_s * 1000).toLocaleTimeString()})` : '',
          L.sample ? ` · sample ${L.sample}` : ''),
        pos.length ? h('div', {class: 'chips'}, pos.map(([k, v]) => h('span', {class: 'chip small'}, `${k} = ${fmt(v)}`))) : null,
        running && L.suspended ? h('div', {class: 'suspended'}, `⏸ held automatically: ${L.suspended} - continues when it clears`) : null),
      h('div', {class: 'b-actions'},
        running && s.re_state !== 'paused' ? [h('button', {onclick: () => runAction('pause'), title: 'pause at the next checkpoint'}, '⏸ Pause')] : null,
        running && s.re_state === 'paused' ? h('button', {class: 'primary', onclick: () => runAction('resume')}, '▶ Resume') : null,
        running ? h('button', {onclick: () => { if (confirm('End this run now? It is kept and marked success.')) runAction('re_stop'); }}, '⏹ Stop') : null,
        running ? h('button', {class: 'danger', onclick: () => { if (confirm('Abort this run (marked aborted)?')) runAction('abort'); }}, '✖ Abort') : null,
        s.items_in_queue && !s.queue_stop_pending && s.manager_state !== 'idle' ? h('button', {class: 'ghost', onclick: () => runAction('stop'), title: 'finish this plan, then stop the queue'}, 'Stop queue after this') : null));
  };
  let lastDetail = 0;
  const update = async () => {
    drawBanner();
    const L = S.live || {};
    if (!L.uid) return;
    if (L.uid !== uid) {
      uid = L.uid; plan = L.plan_name;
      detail = await api(`/api/runs/${uid}`).catch(() => null);
      lastDetail = Date.now();
      grid.set((detail && detail.plots) || L.plots || []);
      return;
    }
    if (!L.exit_status || Date.now() - lastDetail < 8000) {
      if (Date.now() - lastDetail > 8000) { detail = await api(`/api/runs/${uid}`).catch(() => detail); lastDetail = Date.now(); }
      grid.refresh();
    }
  };
  const onPoll = () => update();
  document.addEventListener('fastq:poll', onPoll); cleanups.push(() => document.removeEventListener('fastq:poll', onPoll));
  update();
  return () => cleanups.forEach(f => f());
};

// ================================================================== Experiments: plans, scan builder, presets, queue
// device parameters: named after a lab role (temperature=, lockin=, ...; /api/lab role_names) or a device list
const DEVICE_LISTS = new Set(['detectors', 'readables', 'monitors', 'cameras']);
const isDeviceParam = name => DEVICE_LISTS.has(name) || (S.lab.role_names || []).includes(name);
const moduleLabel = m => (m || '').replace(/^experiments\./, '').replace(/^fastq\.plans\./, '') || 'other';

Pages.launch = async (root, params) => {
  await loadDevices();
  const cleanups = [];
  const list = h('div', {class: 'panel plan-list'}), form = h('div', {class: 'panel'}), qcol = h('div', {});
  root.append(h('div', {class: 'launch'}, list, form, qcol));
  const allowed = ((await tryApi(() => api('/api/queue/plans'), 'plans')) || {}).plans_allowed || {};
  const envDevices = (await api('/api/queue/devices').catch(() => ({devices: []}))).devices;
  let presets = await api('/api/presets').catch(() => ({}));
  // ---- plan list
  const search = h('input', {placeholder: 'search plans…'});
  const ul = h('div', {class: 'plans'});
  const groups = {};
  Object.values(allowed).forEach(p => (groups[moduleLabel(p.module)] = groups[moduleLabel(p.module)] || []).push(p));
  const drawList = () => {
    ul.innerHTML = '';
    const q = search.value.trim().toLowerCase();
    ul.append(h('a', {class: 'plan builder' + (current === '__builder' ? ' sel' : ''), onclick: () => select('__builder')}, h('b', {}, '🛠 Scan builder'),
      h('div', {class: 'small muted'}, 'any axes × any detectors (nd_scan)')));
    // recipes = shipped presets (control/experiments/presets.yaml): a plan with fixed parameters
    const recipes = Object.entries(presets).flatMap(([plan, ps]) => Object.entries(ps).filter(([, x]) => x.builtin && allowed[plan])
      .map(([name, x]) => ({plan, name, ...x}))).filter(r => !q || (r.name + r.plan + (r.comment || '')).toLowerCase().includes(q));
    const bySetup = {};
    recipes.forEach(r => (bySetup[r.setup || 'general'] = bySetup[r.setup || 'general'] || []).push(r));
    for (const [setup, rs] of Object.entries(bySetup).sort()) {
      ul.append(h('div', {class: 'group'}, `recipes · ${setup}`));
      rs.sort((a, b) => a.name.localeCompare(b.name)).forEach(r => ul.append(h('a', {class: 'plan recipe', onclick: () => {
        select(r.plan); setTimeout(() => fillForm && fillForm({...r.kwargs, comment: r.comment}), 50); toast(`recipe ${r.name}`);
      }}, h('b', {}, r.name), h('div', {class: 'small muted'}, `${r.plan} · ${(r.comment || '').slice(0, 80)}`))));
    }
    for (const g of Object.keys(groups).sort((a, b) => (a === 'general') - (b === 'general') || a.localeCompare(b))) {
      const ps = groups[g].filter(p => !q || p.name.toLowerCase().includes(q) || (p.description || '').toLowerCase().includes(q));
      if (!ps.length) continue;
      ul.append(h('div', {class: 'group'}, g));
      ps.sort((a, b) => a.name.localeCompare(b.name)).forEach(p => ul.append(h('a', {class: 'plan' + (p.name === current ? ' sel' : ''), onclick: () => select(p.name)},
        h('b', {}, p.name), h('div', {class: 'small muted'}, (p.description || '').split('\n')[0].slice(0, 90)))));
    }
  };
  search.addEventListener('input', drawList);
  list.append(h('div', {class: 'panel-head'}, h('b', {}, 'Plans'), h('span', {class: 'grow'}),
    h('button', {class: 'icon', title: 're-read control/experiments/*.py and re-discover devices', onclick: async ev => {
      ev.target.disabled = true;
      const r = await tryApi(() => post('/api/queue/reload_plans'), 'reload plans');
      if (r) { toast(`${r.plans} plans loaded`); render(); } else ev.target.disabled = false;
    }}, '⟳')), search, ul);
  let current = params.get('plan') || store.get('lastPlan', '__builder');
  const select = name => { current = name; store.set('lastPlan', name); drawList(); drawForm(); };

  // ---- queue column
  const qPanel = h('div', {class: 'panel'}), hPanel = h('div', {class: 'panel'}), con = h('pre', {class: 'console'});
  qcol.append(qPanel, hPanel, h('details', {class: 'panel'}, h('summary', {}, 'Console'), con));
  const itemText = it => `${it.name}(${Object.entries(it.kwargs || {}).filter(([k]) => k !== 'comment').map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ')})`;
  const drawQueue = async () => {
    const s = S.status;
    const d = await api('/api/queue/items').catch(() => null);
    qPanel.innerHTML = '';
    qPanel.append(h('div', {class: 'panel-head'}, h('b', {}, 'Queue'), h('span', {class: 'muted small'}, s ? `${s.manager_state} · RE ${s.re_state}` : 'offline'),
      h('span', {class: 'grow'}),
      s && s.manager_state === 'idle' ? h('button', {class: 'primary small', disabled: !s.items_in_queue, onclick: () => runAction('start')}, '▶ Start') : null,
      s && s.manager_state !== 'idle' && !s.queue_stop_pending ? h('button', {class: 'small', onclick: () => runAction('stop')}, 'Stop after current') : null,
      s && !s.worker_environment_exists ? h('button', {class: 'small', onclick: () => runAction('env_open')}, 'Open env.') : null));
    if (!d) return;
    const run = d.queue.running_item;
    if (run && run.name) qPanel.append(h('div', {class: 'qitem running'}, '▶ ', h('code', {}, itemText(run)), run.kwargs && run.kwargs.comment ? h('div', {class: 'small muted'}, run.kwargs.comment) : null));
    d.queue.items.forEach((it, i) => qPanel.append(h('div', {class: 'qitem'}, h('span', {class: 'muted'}, `${i + 1}.`), ' ', h('code', {}, itemText(it)),
      it.kwargs && it.kwargs.comment ? h('div', {class: 'small muted'}, it.kwargs.comment) : null,
      h('div', {class: 'qbtn'},
        i > 0 ? h('button', {class: 'icon', title: 'move up', onclick: async () => { await tryApi(() => post('/api/queue/move', {uid: it.item_uid, before_uid: d.queue.items[i - 1].item_uid}), 'move'); drawQueue(); }}, '↑') : null,
        h('button', {class: 'icon', title: 'edit: load into the form', onclick: () => { select(it.name); setTimeout(() => fillForm && fillForm(it.kwargs), 50); }}, '✎'),
        h('button', {class: 'icon', title: 'remove', onclick: async () => { await tryApi(() => post('/api/queue/remove', {uid: it.item_uid}), 'remove'); drawQueue(); }}, '✕')))));
    if (!d.queue.items.length && !(run && run.name)) qPanel.append(h('p', {class: 'muted small'}, 'Queue is empty.'));
    hPanel.innerHTML = '';
    hPanel.append(h('div', {class: 'panel-head'}, h('b', {}, 'Recent')));
    [...d.history.items].reverse().slice(0, 12).forEach(it => {
      const r = it.result || {};
      hPanel.append(h('div', {class: 'qitem'}, statusPill(r.exit_status), ' ', h('code', {}, it.name), ' ',
        (r.run_uids || []).map(u => h('a', {href: `#runs?uid=${u}`}, '→ run ')),
        h('button', {class: 'icon', title: 'load into the form', onclick: () => { select(it.name); setTimeout(() => fillForm && fillForm(it.kwargs), 50); }}, '↺'),
        r.msg ? h('div', {class: 'small bad-text'}, r.msg.slice(0, 200)) : null));
    });
  };
  const onPoll = () => drawQueue();
  document.addEventListener('fastq:poll', onPoll); cleanups.push(() => document.removeEventListener('fastq:poll', onPoll));
  drawQueue();
  let cws, closed = false;
  const openConsole = () => {
    cws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/console');
    cws.onmessage = ev => { const end = con.scrollTop + con.clientHeight >= con.scrollHeight - 20; con.textContent = (con.textContent + ev.data).slice(-100000); if (end) con.scrollTop = con.scrollHeight; };
    cws.onclose = () => { if (!closed) setTimeout(openConsole, 3000); };
  };
  openConsole(); cleanups.push(() => { closed = true; cws.close(); });

  // ---- submit helpers
  const queue = async (name, kwargs, start) => {
    const r = await tryApi(() => post('/api/queue/add', {name, kwargs}), 'queue');
    if (!r) return;
    if (r.success === false) { toast(r.msg, true); return; }
    toast(`${name} queued`);
    if (start && S.status && S.status.manager_state === 'idle') await runAction('start');
    drawQueue();
  };
  const presetBar = (plan, getKwargs, setKwargs) => {
    const sel = h('select', {}, h('option', {value: ''}, '▾ presets'));
    const fillSel = () => { sel.innerHTML = ''; sel.append(h('option', {value: ''}, '▾ presets')); Object.keys(presets[plan] || {}).sort().forEach(n => sel.append(h('option', {value: n}, n))); };
    fillSel();
    sel.addEventListener('change', () => { const p = (presets[plan] || {})[sel.value]; if (p) { setKwargs({...p.kwargs, comment: p.comment}); toast(`preset ${sel.value} loaded`); } });
    const fromRun = h('select', {style: 'max-width:180px'}, h('option', {value: ''}, '↺ from run…'));
    api('/api/runs?limit=40').then(rs => rs.forEach(r => fromRun.append(h('option', {value: r.uid}, `${r.scan_id} ${r.plan_name}${r.comment ? ' – ' + r.comment.slice(0, 30) : ''}`)))).catch(() => {});
    fromRun.addEventListener('change', async () => {
      const req = fromRun.value && await tryApi(() => api(`/api/runs/${fromRun.value}/request`), 'parameters');
      fromRun.value = '';
      if (!req) return;
      if ((req.renamed || []).length) toast(`older run - renamed: ${req.renamed.join(', ')}`);
      if (req.name !== plan && allowed[req.name]) { select(req.name); setTimeout(() => fillForm(req.kwargs), 50); return; }
      setKwargs(req.kwargs); toast('parameters of the run filled in');
    });
    return h('div', {class: 'row small'}, sel,
      h('button', {class: 'ghost', onclick: async () => {
        const name = prompt('Save these parameters as preset:', sel.value || ''); if (!name) return;
        const kw = getKwargs(); if (!kw) return;
        const {comment, ...rest} = kw;
        if (await tryApi(() => put(`/api/presets/${encodeURIComponent(plan)}/${encodeURIComponent(name)}`, {kwargs: rest, comment: comment || ''}), 'preset')) {
          presets = await api('/api/presets').catch(() => presets); fillSel(); sel.value = name; toast('preset saved');
        }
      }}, '💾 Save'),
      h('button', {class: 'ghost', disabled: false, onclick: async () => {
        if (!sel.value || !confirm(`Delete preset ${sel.value}?`)) return;
        await tryApi(() => del(`/api/presets/${encodeURIComponent(plan)}/${encodeURIComponent(sel.value)}`), 'delete');
        presets = await api('/api/presets').catch(() => presets); fillSel();
      }}, '🗑'), h('span', {class: 'grow'}), fromRun);
  };

  // ---- plan form
  let fillForm = null;
  const drawForm = () => {
    form.innerHTML = '';
    if (current === '__builder') return scanBuilder();
    const spec = allowed[current];
    if (!spec) { form.append(h('p', {class: 'muted'}, 'Choose a plan.')); return; }
    const doc = (spec.description || '').trim();
    form.append(h('div', {class: 'panel-head'}, h('b', {class: 'big'}, spec.name), h('span', {class: 'muted small'}, moduleLabel(spec.module))),
      doc ? h('p', {class: 'doc'}, doc.split('\n\n')[0]) : null,
      doc.includes('\n\n') ? h('details', {}, h('summary', {class: 'small'}, 'more'), h('pre', {class: 'doc small'}, doc)) : null);
    const inputs = {};
    const fields = h('div', {class: 'fields'});
    for (const p of spec.parameters || []) {
      const kind = p.kind && p.kind.name;
      if (kind === 'VAR_POSITIONAL' || kind === 'VAR_KEYWORD' || p.name === 'md' || p.name === 'comment') continue;
      const ann = ((p.annotation || {}).type || '').replace(/'/g, '');
      const def = p.default;
      let inp;
      if (/^bool/.test(ann) || def === 'True' || def === 'False') {
        inp = h('input', {type: 'checkbox', checked: def === 'True'}); inp.dataset.kind = 'bool';
      } else if (isDeviceParam(p.name) || /device/i.test(ann)) {
        const multi = DEVICE_LISTS.has(p.name);
        const roleOpts = Object.entries(S.lab.roles || {}).map(([r, n]) => h('option', {value: '@' + r}, `@${r} (${n})`));
        inp = h('select', {multiple: multi, size: multi ? Math.min(8, envDevices.length + roleOpts.length) : null},
          multi ? null : h('option', {value: ''}, def === 'None' ? '(lab role / none)' : '— choose —'), roleOpts, envDevices.map(n => h('option', {value: n}, n)));
        inp.dataset.kind = multi ? 'devices' : 'device';
      } else {
        inp = h('input', {placeholder: def !== undefined ? String(def) : 'required', class: def === undefined ? 'required' : ''});
        inp.dataset.kind = /int|float/.test(ann) && !/list/.test(ann) ? 'number' : 'value';
      }
      inputs[p.name] = inp;
      fields.append(h('label', {title: ann}, p.name, h('span', {class: 'muted small'}, ann ? ` ${ann}` : '')), inp);
    }
    const comment = h('textarea', {rows: 2, placeholder: 'What is this run? (goes to the run metadata and the ELOG entry)'});
    fields.append(h('label', {}, 'comment'), comment);
    const getKwargs = () => {
      const kw = {};
      for (const [k, inp] of Object.entries(inputs)) {
        const t = inp.dataset.kind;
        if (t === 'bool') { const d = (spec.parameters.find(p => p.name === k) || {}).default; if (String(inp.checked ? 'True' : 'False') !== d) kw[k] = inp.checked; continue; }
        if (t === 'devices') { const v = [...inp.selectedOptions].map(o => o.value); if (v.length) kw[k] = v; continue; }
        if (t === 'device') { if (inp.value) kw[k] = inp.value; continue; }
        const v = parseValue(inp.value);
        if (v !== undefined) kw[k] = v;
      }
      if (comment.value.trim()) kw.comment = comment.value.trim();
      return kw;
    };
    fillForm = kw => {
      for (const [k, inp] of Object.entries(inputs)) {
        const v = (kw || {})[k];
        if (inp.dataset.kind === 'bool') inp.checked = v === undefined ? (spec.parameters.find(p => p.name === k) || {}).default === 'True' : !!v;
        else if (inp.dataset.kind === 'devices') [...inp.options].forEach(o => { o.selected = (Array.isArray(v) ? v : []).includes(o.value); });
        else if (inp.dataset.kind === 'device') inp.value = v || '';
        else inp.value = v === undefined || v === null ? '' : typeof v === 'string' ? v : JSON.stringify(v);
      }
      comment.value = (kw || {}).comment || '';
    };
    form.append(presetBar(spec.name, getKwargs, fillForm), fields,
      h('div', {class: 'row end'}, h('button', {onclick: () => queue(spec.name, getKwargs(), false)}, 'Add to queue'),
        h('button', {class: 'primary', onclick: () => queue(spec.name, getKwargs(), true)}, 'Queue & start')));
  };

  // ---- scan builder (nd_scan)
  const scanBuilder = async () => {
    const schemas = await loadSchemas();
    const devs = S.devices.filter(d => d.enabled && envDevices.includes(d.name));
    const settable = d => { const s = schemas[d.name]; return s ? s.params.filter(p => p.writable && ['float', 'int', 'bool', 'enum'].includes(p.dtype)) : []; };
    const isDet = d => { const s = schemas[d.name]; return s && s.ophyd && (s.ophyd.trigger || s.ophyd.detector); };
    const hasTrigger = d => !!ophydOf(d.name).pulser;
    const axes = store.get('builderAxes', [{device: '', signal: '', mode: 'range', start: 0, stop: 1, num: 11}]);
    let repeat = store.get('builderRepeat', 1);
    const box = h('div');
    const out = h('div', {class: 'muted small'});
    const detSel = h('select', {multiple: true, size: 5}, devs.map(d => h('option', {value: d.name, selected: store.get('builderDets', []).includes(d.name)}, d.name + (isDet(d) ? '  ◆' : ''))));
    const readSel = h('select', {multiple: true, size: 5}, devs.map(d => h('option', {value: d.name, selected: store.get('builderReads', []).includes(d.name)}, d.name)));
    const pulser = h('select', {}, h('option', {value: ''}, '— internal trigger —'), devs.filter(hasTrigger).map(d => h('option', {value: d.name}, d.name)));
    pulser.value = store.get('builderPulser', '');
    const cams = devs.filter(d => schemas[d.name] && (schemas[d.name].ophyd || {}).detector === 'camera');
    const ratio = h('select', {}, h('option', {value: ''}, '—'), cams.flatMap(a => cams.filter(b => b !== a).map(b => h('option', {value: `${a.name}/${b.name}`}, `${a.name} / ${b.name}`))));
    ratio.value = store.get('builderRatio', '');
    const comment = h('textarea', {rows: 2, placeholder: 'What is this run?'});
    const values = a => {
      if (a.mode === 'list') return parseValue(a.list || '[]') || [];
      const n = Math.max(1, Math.round(a.num || 1)), s0 = +a.start, s1 = +a.stop;
      if (a.mode === 'step') { const st = Math.abs(+a.step || 1) * Math.sign(s1 - s0 || 1), k = Math.floor(Math.abs((s1 - s0) / st) + 1e-9) + 1; return [...Array(k).keys()].map(i => s0 + i * st); }
      return n === 1 ? [s0] : [...Array(n).keys()].map(i => s0 + (s1 - s0) * i / (n - 1));
    };
    const draw = () => {
      box.innerHTML = '';
      const t = h('div', {class: 'axes'});
      t.append(h('div', {class: 'axis head'}, h('span', {}, 'axis (outermost first)'), h('span', {}, 'values'), h('span', {}, 'options'), h('span', {})));
      t.append(h('div', {class: 'axis'}, h('span', {}, '🔁 repeat'), h('span', {}, h('input', {type: 'number', value: repeat, min: 0, style: 'width:90px', onchange: e => { repeat = +e.target.value; changed(); }}),
        h('span', {class: 'small muted'}, ' × (0 = until Stop, 1 = no repeat)')), h('span', {}), h('span', {})));
      axes.forEach((a, i) => {
        const dsel = h('select', {onchange: e => { a.device = e.target.value; a.signal = ''; draw(); changed(); }}, h('option', {value: ''}, '— device —'),
          devs.filter(d => settable(d).length || !schemas[d.name]).map(d => h('option', {value: d.name}, d.name)));
        dsel.value = a.device;
        const dev = devs.find(d => d.name === a.device);
        const ps = dev ? settable(dev) : [];
        const pos = dev && schemas[dev.name] && (schemas[dev.name].params || []).filter(p => p.positioner);
        const ssel = h('select', {onchange: e => { a.signal = e.target.value; changed(); }},
          pos && pos.length === 1 ? h('option', {value: ''}, `${pos[0].name} (device)`) : h('option', {value: ''}, '(device itself)'),
          ps.map(p => h('option', {value: p.attr}, `${p.name}${p.units ? ' [' + p.units + ']' : ''}`)));
        ssel.value = a.signal || '';
        const num = (k, w = 70) => h('input', {type: 'number', step: 'any', value: a[k] ?? '', style: `width:${w}px`, onchange: e => { a[k] = e.target.value === '' ? undefined : +e.target.value; changed(); }});
        const mode = h('select', {onchange: e => { a.mode = e.target.value; draw(); changed(); }}, h('option', {value: 'range'}, 'from-to, N'), h('option', {value: 'step'}, 'from-to, step'), h('option', {value: 'list'}, 'list'));
        mode.value = a.mode;
        const vals = a.mode === 'list' ? h('input', {value: a.list || '', placeholder: '[0, 1, 2] or JSON', onchange: e => { a.list = e.target.value; changed(); }})
          : h('span', {}, num('start'), ' → ', num('stop'), a.mode === 'step' ? [' step ', num('step')] : [' N ', num('num', 55)]);
        const chk = (k, label, title) => h('label', {class: 'small', title}, h('input', {type: 'checkbox', checked: !!a[k], onchange: e => { a[k] = e.target.checked; changed(); }}), ' ' + label);
        t.append(h('div', {class: 'axis'}, h('span', {}, dsel, ssel), h('span', {}, mode, vals),
          h('span', {}, chk('snake', 'snake', 'reverse on every other pass'), chk('relative', 'relative', 'values relative to the start position'),
            h('span', {class: 'small muted'}, ' tol'), num('tolerance', 55), h('span', {class: 'small muted'}, ' settle s'), num('settle_s', 50)),
          h('span', {}, i > 0 ? h('button', {class: 'icon', onclick: () => { axes.splice(i - 1, 0, axes.splice(i, 1)[0]); draw(); changed(); }}, '↑') : null,
            h('button', {class: 'icon', onclick: () => { axes.splice(i, 1); draw(); changed(); }}, '✕'))));
      });
      box.append(t, h('div', {class: 'row small'}, h('button', {class: 'ghost', onclick: () => { axes.push({device: '', mode: 'range', start: 0, stop: 1, num: 11}); draw(); changed(); }}, '+ axis')));
    };
    const kwargs = () => {
      const ax = [];
      if (repeat !== 1) ax.push({repeat});
      for (const a of axes) {
        if (!a.device) continue;
        const e = {device: a.device};
        if (a.signal) e.signal = a.signal;
        if (a.mode === 'list') e.values = parseValue(a.list || '[]');
        else if (a.mode === 'step') Object.assign(e, {start: a.start, stop: a.stop, step: a.step});
        else Object.assign(e, {start: a.start, stop: a.stop, num: a.num});
        for (const k of ['snake', 'relative']) if (a[k]) e[k] = true;
        for (const k of ['tolerance', 'settle_s']) if (a[k] != null && a[k] !== '') e[k] = a[k];
        ax.push(e);
      }
      const kw = {axes: ax, detectors: [...detSel.selectedOptions].map(o => o.value), readables: [...readSel.selectedOptions].map(o => o.value)};
      if (pulser.value) kw.pulser = pulser.value;
      if (ratio.value) kw.ratios = [ratio.value.split('/')];
      if (comment.value.trim()) kw.comment = comment.value.trim();
      return kw;
    };
    const changed = () => {
      store.set('builderAxes', axes); store.set('builderRepeat', repeat);
      store.set('builderDets', [...detSel.selectedOptions].map(o => o.value)); store.set('builderReads', [...readSel.selectedOptions].map(o => o.value));
      store.set('builderPulser', pulser.value); store.set('builderRatio', ratio.value);
      const n = axes.filter(a => a.device).reduce((m, a) => m * values(a).length, 1);
      out.textContent = repeat === 0 ? `${n} points per pass, repeated until Stop` : `${n * Math.max(1, repeat)} points`;
    };
    [detSel, readSel, pulser, ratio].forEach(e => e.addEventListener('change', changed));
    const setKw = kw => {
      axes.splice(0); repeat = 1;
      for (const a of kw.axes || []) {
        if ('repeat' in a) { repeat = a.repeat; continue; }
        const m = a.values ? 'list' : a.step != null ? 'step' : 'range';
        axes.push({...a, mode: m, list: a.values ? JSON.stringify(a.values) : undefined});
      }
      [...detSel.options].forEach(o => { o.selected = (kw.detectors || []).includes(o.value); });
      [...readSel.options].forEach(o => { o.selected = (kw.readables || []).includes(o.value); });
      pulser.value = kw.pulser || ''; ratio.value = kw.ratios && kw.ratios[0] ? kw.ratios[0].join('/') : '';
      comment.value = kw.comment || ''; draw(); changed();
    };
    fillForm = setKw;
    form.append(h('div', {class: 'panel-head'}, h('b', {class: 'big'}, '🛠 Scan builder'), h('span', {class: 'muted small'}, 'nd_scan: nested axes, set-and-confirm moves, every point = all detectors')),
      presetBar('nd_scan', kwargs, setKw), box,
      h('div', {class: 'fields'},
        h('label', {}, 'detectors', h('span', {class: 'muted small'}, ' triggered at every point (◆ = detector)')), detSel,
        h('label', {}, 'also read'), readSel,
        h('label', {}, 'trigger pulser', h('span', {class: 'muted small'}, ' cameras armed, pulser fires until all frames are in')), pulser,
        cams.length > 1 ? [h('label', {}, 'ROI ratio'), ratio] : null,
        h('label', {}, 'comment'), comment),
      h('div', {class: 'row end'}, out, h('span', {class: 'grow'}),
        h('button', {onclick: () => queue('nd_scan', kwargs(), false)}, 'Add to queue'),
        h('button', {class: 'primary', onclick: () => queue('nd_scan', kwargs(), true)}, 'Queue & start')));
    draw(); changed();
  };

  drawList(); drawForm();
  if (params.get('from')) {
    const req = await tryApi(() => api(`/api/runs/${params.get('from')}/request`), 'parameters');
    if (req && (req.renamed || []).length) toast(`older run - renamed: ${req.renamed.join(', ')}`);
    if (req && allowed[req.name]) { select(req.name); setTimeout(() => fillForm && fillForm(req.kwargs), 80); }
  }
  return () => cleanups.forEach(f => f());
};

// ================================================================== Runs: search, run view, compare
Pages.runs = async (root, params) => {
  if (params.get('uid')) return runView(root, params.get('uid'));
  if (params.get('compare')) return compareView(root, params.get('compare').split(','));
  const q = h('input', {placeholder: 'search: plan, comment, sample, kind, run number…', value: params.get('q') || '', style: 'flex:1'});
  const status = h('select', {}, ['', 'success', 'running', 'abort', 'fail'].map(s => h('option', {value: s}, s || 'any status')));
  const t = h('table', {class: 'runs'});
  const chosen = new Set();
  const cmp = h('button', {disabled: true, onclick: () => { location.hash = `#runs?compare=${[...chosen].join(',')}`; }}, 'Compare selected');
  root.append(h('div', {class: 'panel'}, h('div', {class: 'row'}, q, status, cmp), t));
  const draw = async () => {
    const rows = await tryApi(() => api(`/api/runs?limit=200&q=${encodeURIComponent(q.value)}&status=${status.value}`), 'runs') || [];
    t.innerHTML = '';
    t.append(h('tr', {}, h('th', {}), h('th', {}, '#'), h('th', {}, 'plan'), h('th', {}, 'comment'), h('th', {}, 'sample'), h('th', {}, 'started'),
      h('th', {}, 'duration'), h('th', {}, 'points'), h('th', {}, 'status')));
    rows.forEach(r => t.append(h('tr', {class: 'click', onclick: e => { if (e.target.type !== 'checkbox') location.hash = `#runs?uid=${r.uid}`; }},
      h('td', {}, h('input', {type: 'checkbox', checked: chosen.has(r.uid), onchange: e => { e.target.checked ? chosen.add(r.uid) : chosen.delete(r.uid); cmp.disabled = chosen.size < 2; cmp.textContent = `Compare ${chosen.size || ''} selected`; }})),
      h('td', {}, r.scan_id ?? ''), h('td', {}, r.plan_name), h('td', {class: 'cmt'}, r.comment || ''), h('td', {}, r.sample || ''),
      h('td', {class: 'small'}, fmtShort(r.time)), h('td', {class: 'small'}, r.stop_time ? fmtDur(r.stop_time - r.time) : ''),
      h('td', {class: 'small'}, r.num_events != null ? r.num_events : ''),
      h('td', {}, statusPill(r.exit_status || (S.live && S.live.uid === r.uid ? 'running' : 'incomplete'))))));
    if (!rows.length) t.append(h('tr', {}, h('td', {colspan: 9, class: 'muted'}, 'No runs match.')));
  };
  let tmr;
  q.addEventListener('input', () => { clearTimeout(tmr); tmr = setTimeout(draw, 300); });
  status.addEventListener('change', draw);
  draw();
};

async function runView(root, ref) {
  const cleanups = [];
  let d = await tryApi(() => api(`/api/runs/${ref}`), 'run');
  if (!d) return;
  const st = d.start || {};
  const grid = new PlotGrid({uid: () => d.uid, plan: () => d.plan_name, detail: () => d, title: () => `run${d.scan_id}`});
  cleanups.push(() => grid.destroy());
  const streamSel = h('select', {}, Object.keys(d.streams).map(s => h('option', {}, s)));
  const elogLink = d.elog && S.lab.elog.url ? h('a', {href: `${S.lab.elog.url.replace(/\/$/, '')}/${d.elog.logbook}/${d.elog.msg_id}`, target: '_blank'}, `📓 ${d.elog.logbook} #${d.elog.msg_id}`) : null;
  const frames = Object.entries(d.streams.primary || {}).filter(([, v]) => ((v || {}).shape || []).length > 1).map(([k]) => k);
  const frameBox = h('div', {class: 'plot-grid'});
  // the plan code changed since this run: offer the exact archived version as its own plan
  const restoreBtn = h('span');
  api(`/api/runs/${d.uid}/request`).then(r => {
    if (!r.code_changed || S.lab.read_only) return;
    restoreBtn.append(h('button', {class: 'ghost', title: `this run used code version ${r.code.version}; the plan has changed since`, onclick: async () => {
      const x = await tryApi(() => post(`/api/runs/${d.uid}/restore_plan`, {}), 'restore');
      if (x) { toast(`plan ${x.plan} restored - in the plan list now`); location.hash = `#launch?from=${d.uid}`; }
    }}, '⟲ Restore exact code'));
  }).catch(() => {});
  root.append(h('div', {class: 'banner'},
    h('div', {class: 'b-main'},
      h('div', {class: 'b-title'}, statusPill(d.exit_status), ' ', runTitle(d), d.comment ? h('span', {class: 'muted'}, ' — ' + d.comment) : null),
      h('div', {class: 'small muted'}, `${fmtTime(d.time)}${d.stop_time ? ' · ' + fmtDur(d.stop_time - d.time) : ''}${d.sample ? ' · sample ' + d.sample : ''}`,
        d.num_events != null ? ` · ${d.num_events} points` : '', ' · ', h('code', {}, d.uid.slice(0, 8)), ' ', elogLink)),
    h('div', {class: 'b-actions'},
      h('a', {class: 'btn', href: `#launch?from=${d.uid}`, title: 'open the plan with these parameters'}, '↺ Run again'), restoreBtn,
      h('button', {onclick: () => modal('Comment on run ' + d.scan_id, composer({runUid: d.uid}))}, '📓 Comment'),
      h('span', {class: 'btn-group'}, streamSel, h('button', {onclick: () => { location.href = `/api/runs/${d.uid}/csv?stream=${encodeURIComponent(streamSel.value)}`; }}, '⬇ CSV')),
      h('button', {title: 'all streams + metadata in one HDF5 file (frames stay referenced)', onclick: () => { location.href = `/api/runs/${d.uid}/hdf5`; }}, '⬇ HDF5'),
      h('a', {class: 'btn ghost', href: '#runs'}, '← all runs'))),
  grid.root, frames.length ? h('details', {class: 'panel', ontoggle: e => { if (e.target.open && !frameBox.firstChild) frames.forEach(k => frameBox.append(framePanel(d.uid, k, cleanups))); }},
    h('summary', {}, `Camera frames (${frames.join(', ')})`), frameBox) : null,
  h('details', {class: 'panel'}, h('summary', {}, 'Metadata'), st.plan_request ? h('pre', {class: 'small'}, `${st.plan_request.name}(${Object.entries(st.plan_request.kwargs || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ')})`) : null,
    h('pre', {class: 'small'}, JSON.stringify(st, null, 1))));
  grid.set(d.plots);
  if (!d.exit_status) {
    const ti = setInterval(async () => {
      const dd = await api(`/api/runs/${d.uid}`).catch(() => null);
      if (dd) d = dd;
      grid.refresh();
      if (d.exit_status) clearInterval(ti);
    }, 3000);
    cleanups.push(() => clearInterval(ti));
  }
  return () => cleanups.forEach(f => f());
}

async function compareView(root, uids) {
  const cleanups = [];
  const details = (await Promise.all(uids.map(u => api(`/api/runs/${u}`).catch(() => null)))).filter(Boolean);
  if (!details.length) return;
  let spec = details.map(d => (d.plots || [])[0]).find(Boolean) || {x: 'time', y: []};
  const body = h('div', {class: 'plot-body tall'}), plot = new Plot(body, {alwaysLegend: true});
  cleanups.push(() => plot.destroy());
  const offset = h('input', {type: 'checkbox'});
  const editBox = h('div');
  const draw = async () => {
    const all = [];
    for (const [i, d] of details.entries()) {
      const r = await api(`/api/runs/${d.uid}/plot`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(spec)}).catch(() => null);
      if (!r) continue;
      if (r.kind === 'heatmap') { toast('maps cannot be overlaid - choose a line plot', true); return; }
      r.series.forEach((s, k) => {
        const y = offset.checked ? s.y.map(v => v == null ? v : v + i * (offset.step || 0)) : s.y;
        all.push({...s, y, name: `${d.scan_id}${d.comment ? ' ' + d.comment.slice(0, 25) : ''}${r.series.length > 1 ? ' · ' + s.name : ''}`, color: r.series.length > 1 ? undefined : PALETTE[i % PALETTE.length]});
      });
      plot.o.xLabel = r.x_label; plot.o.yLabel = r.y_label;
    }
    plot.setSeries(all, {mode: spec.kind === 'scatter' ? 'markers' : null});
  };
  const src = {streams: () => Object.keys(details[0].streams), columns: st => { const dk = details[0].streams[st] || {}; return ['time', 'seq_num', ...Object.keys(dk).filter(k => ((dk[k] || {}).shape || []).length <= 1).sort()]; }};
  root.append(h('div', {class: 'banner'}, h('div', {class: 'b-main'}, h('div', {class: 'b-title'}, `Compare ${details.length} runs`),
    h('div', {class: 'chips'}, details.map((d, i) => h('a', {class: 'chip small', href: `#runs?uid=${d.uid}`, style: `border-color:${PALETTE[i % PALETTE.length]}`}, runTitle(d))))),
  h('div', {class: 'b-actions'}, h('button', {onclick: () => { editBox.innerHTML = ''; editBox.append(specEditor(spec, src.columns, src.streams(), out => { editBox.innerHTML = ''; if (out) { spec = out; draw(); } })); }}, '✎ What to plot'),
    h('a', {class: 'btn ghost', href: '#runs'}, '← all runs'))),
  editBox, h('div', {class: 'panel'}, body));
  draw();
  return () => cleanups.forEach(f => f());
}

// ================================================================== Devices
// A control row. Safety (IOC schema, lab overrides; enforced by the server): safe = written at once,
// confirm = dialog, protected = PIN. Enter in a field presses its button; focusing selects the value.
function controlRow(dev, c) {
  const pv = dev.prefix + c.write_suffix, label = c.label || c.write_suffix, safety = controlSafety(dev, c.write_suffix, c.safety);
  const lim = c.limits ? ` (${c.limits[0]} … ${c.limits[1]})` : '';
  const badge = SAFETY_BADGE[safety] ? h('span', {class: 'safety ' + safety, title: safety === 'protected' ? 'protected: needs the lab PIN' : 'asks for confirmation'}, SAFETY_BADGE[safety]) : null;
  const lbl = h('label', {title: [c.doc, c.write_suffix, safety].filter(Boolean).join(' · ')}, badge, label);
  const put = (v, text) => putPV(pv, v, text || label, safety);
  const rb = c.readback_suffix ? pvCell(dev.prefix + c.readback_suffix, {precision: c.precision}) : null;
  if (c.type === 'setpoint') {
    const inp = h('input', {type: 'number', step: 'any', style: 'width:120px', placeholder: lim.trim()});
    return h('div', {class: 'ctl'}, lbl, rb, inp, h('button', {onclick: () => { if (inp.value !== '') put(parseFloat(inp.value), `${label} → ${inp.value}`); }}, 'Set'));
  }
  if (c.type === 'enum') {
    const sel = h('select', {}, c.choices.map(x => h('option', {}, x)));
    return h('div', {class: 'ctl'}, lbl, rb, sel, h('button', {onclick: () => put(c.choices.indexOf(sel.value), `${label} → ${sel.value}`)}, 'Set'));
  }
  if (c.type === 'toggle') {
    const cb = h('input', {type: 'checkbox'});
    const cell = pvCell(dev.prefix + (c.readback_suffix || c.write_suffix), {onValue: v => { cb.checked = truthy(v); }});
    cb.addEventListener('change', async () => { const want = cb.checked; if (!await put(want ? 1 : 0, `${label} → ${want ? 'on' : 'off'}`)) cb.checked = !want; });
    return h('div', {class: 'ctl'}, lbl, cb, cell);
  }
  if (c.type === 'text') {
    const inp = h('input', {style: 'flex:1'});
    return h('div', {class: 'ctl'}, lbl, rb, inp, h('button', {onclick: () => put(inp.value, `${label} → ${inp.value}`)}, 'Set'));
  }
  if (c.type === 'action') {
    const inp = c.arg ? h('input', {type: c.arg === 'str' ? 'text' : 'number', step: 'any', style: 'width:110px', placeholder: c.arg}) : null;
    const run = () => {
      const v = !c.arg ? 1 : c.arg === 'str' ? inp.value : parseFloat(inp.value);
      if (c.arg && (inp.value === '' || (c.arg !== 'str' && isNaN(v)))) return;
      put(v, `${label}${c.doc ? ' - ' + c.doc : ''}`);
    };
    return h('div', {class: 'ctl'}, lbl, inp, h('button', {class: safety === 'protected' ? 'danger' : '', onclick: run}, c.arg ? 'Run' : label));
  }
  return h('div', {class: 'muted'}, `unknown control ${c.type}`);
}

// small pop-up menu under an element: items = [[label, fn, disabled]]
function popMenu(anchor, items) {
  document.querySelectorAll('.popmenu').forEach(m => m.remove());
  const r = anchor.getBoundingClientRect();
  const m = h('div', {class: 'popmenu', style: `left:${Math.min(r.left, innerWidth - 230)}px;top:${r.bottom + 2}px`},
    items.map(([l, fn, off]) => h('button', {class: 'ghost', disabled: !!off, onclick: () => { m.remove(); fn(); }}, l)));
  document.body.append(m);
  setTimeout(() => document.addEventListener('click', function away(e) { if (!m.contains(e.target)) { m.remove(); document.removeEventListener('click', away); } }), 0);
}

function historyBox(dev) {
  const box = h('details', {class: 'panel'}, h('summary', {}, h('b', {}, 'History'), h('span', {class: 'muted small'}, ' how long this device\'s values are kept')));
  box.addEventListener('toggle', async () => {
    if (!box.open || box.dataset.loaded) return;
    box.dataset.loaded = 1;
    const s = await api('/api/history/settings'), mine = s.devices[dev.name] || {}, def = s.default;
    const f = {period_s: ['sample every (s)', 'one raw value per period, when it changed'], raw_days: ['raw values (days)', 'full resolution'],
      keep_days: ['envelopes (days)', 'min / mean / max per minute']};
    const ins = {};
    const rows = Object.entries(f).map(([k, [l, doc]]) => h('div', {class: 'ctl', title: doc}, h('label', {}, l),
      ins[k] = h('input', {type: 'number', step: 'any', min: 0, style: 'width:100px', value: mine[k] ?? '', placeholder: `global ${def[k]}`})));
    const save = async reset => {
      const cur = await api('/api/history/settings');
      const d = {}; if (!reset) for (const k in ins) if (ins[k].value !== '') d[k] = +ins[k].value;
      cur.devices[dev.name] = d;
      if (await tryApi(() => put('/api/history/settings', {default: cur.default, devices: cur.devices}), 'history settings')) {
        toast(reset ? 'global history settings' : 'history settings saved'); if (reset) for (const k in ins) ins[k].value = '';
      }
    };
    box.append(h('div', {class: 'enter-scope'}, rows, h('div', {class: 'row'}, h('button', {class: 'primary', onclick: () => save(false)}, 'Save'),
      h('button', {class: 'ghost', onclick: () => save(true)}, 'Use global')),
      h('p', {class: 'small muted'}, 'Empty = the global setting (System page). Long views use the minute envelopes, so months stay cheap.')));
  });
  return box;
}

function derivedEditor(dev, schemas) {
  const panel = h('div', {class: 'panel'});
  const inputsList = h('datalist', {id: 'derived-inputs'}, Object.entries(schemas).flatMap(([n, s]) => (s.params || [])
    .filter(p => ['float', 'int'].includes(p.dtype) && p.readable !== false && n !== dev.name).map(p => h('option', {value: `${n}.${p.attr}`}))));
  const kv = o => Object.entries(o || {}).map(([k, v]) => `${k}=${v}`).join(', ');
  const parseKv = t => Object.fromEntries(t.split(',').map(x => x.trim()).filter(Boolean).map(x => { const [k, ...v] = x.split('='); return [k.trim(), v.join('=').trim()]; }));
  let defs = [];
  const list = h('div');
  const draw = () => {
    list.innerHTML = '';
    defs.forEach((d, i) => {
      const f = (k, ph, w, conv = x => x) => h('input', {value: d[k] == null ? '' : (typeof d[k] === 'object' ? kv(d[k]) : d[k]), placeholder: ph, style: `width:${w}`,
        list: k === 'inputs' ? 'derived-inputs' : null, onchange: e => { d[k] = conv(e.target.value); }});
      list.append(h('div', {class: 'derived-def'},
        h('div', {class: 'row'}, f('name', 'Name (CamelCase)', '150px'), f('units', 'units', '70px'), f('expr', 'formula, e.g. x / ((amp / sqrt(2) - x) / series_r)', '100%'),
          h('button', {class: 'icon', title: 'remove', onclick: () => { defs.splice(i, 1); draw(); }}, '✕')),
        h('div', {class: 'row'}, h('span', {class: 'small muted'}, 'inputs'), f('inputs', 'x=mfli1.x, amp=mfli1.amplitude', '45%', parseKv),
          h('span', {class: 'small muted'}, 'constants'), f('constants', 'series_r=6.7e6', '25%', parseKv)),
        h('div', {class: 'row'}, f('doc', 'how / why it is derived (shown with the value and stored in every run)', '100%'))));
    });
  };
  const load = async () => { const r = await api('/api/derived'); defs = r.values; draw(); fns.textContent = `functions: ${r.functions.join(', ')}; constants: ${r.constants.join(', ')}`; };
  const fns = h('p', {class: 'small muted'});
  panel.append(h('div', {class: 'panel-head'}, h('b', {}, 'ƒ Derived values of this experiment'), h('span', {class: 'grow'}),
    h('button', {class: 'ghost', onclick: () => { defs.push({name: '', expr: '', inputs: {}, constants: {}, units: '', doc: ''}); draw(); }}, '+ Value'),
    h('button', {class: 'primary', onclick: async () => {
      const r = await tryApi(() => put('/api/derived', {values: defs}), 'derived values');
      if (r) { toast('saved - the live values restart in a few seconds'); defs = r.values; draw(); }
    }}, 'Save')),
    h('p', {class: 'small muted'}, 'Instruments publish what they measure; quantities computed from it with the setup\'s parameters are defined here. ' +
      'Every run records these definitions; plots and exports compute them from the raw data of that run.'), inputsList, list, fns);
  load();
  return panel;
}

Pages.devices = async (root, params) => {
  await loadDevices();
  const cleanups = [];
  const list = h('div', {class: 'panel dev-list'}), detail = h('div', {});
  root.append(h('div', {class: 'devices'}, list, detail));
  const search = h('input', {placeholder: 'filter…', value: params.get('f') || ''});
  const ul = h('div');
  const byHost = {};
  S.devices.forEach(d => (byHost[d.host || '?'] = byHost[d.host || '?'] || []).push(d));
  const drawList = () => {
    ul.innerHTML = '';
    const q = search.value.toLowerCase();
    for (const [host, devs] of Object.entries(byHost)) {
      const shown = devs.filter(d => !q || (d.name + d.type + rolesOf(d.name).join(' ')).toLowerCase().includes(q));
      if (!shown.length) continue;
      ul.append(h('div', {class: 'group'}, `🖥 ${host}`));
      shown.forEach(d => {
        const dot = h('span', {class: 'dot'});
        if (d.enabled) cleanups.push(PV.sub([d.prefix + 'Connected'], p => { dot.className = 'dot ' + (!p.connected ? 'bad' : truthy(p.value) ? 'ok' : 'warn'); }));
        ul.append(h('a', {class: 'dev' + (params.get('d') === d.name ? ' sel' : ''), href: `#devices?d=${d.name}`}, dot, h('b', {}, d.name), ' ',
          h('span', {class: 'small muted'}, d.type), rolesOf(d.name).map(r => h('span', {class: 'pill small'}, r))));
      });
    }
  };
  search.addEventListener('input', drawList);
  const down = (S.dirErrors || []).map(d => h('div', {class: 'small bad-text'},
    `directory ${d} not reachable - is that IOC host (start_ioc_host) running?`));
  list.append(search, ...down, ul); drawList();
  const dev = S.devices.find(d => d.name === params.get('d'));
  if (!dev) {
    detail.append(h('div', {class: 'panel'}, h('p', {class: 'muted'}, 'Choose a device. Every device is an IOC on the PC it is wired to; it describes itself, so this page is generated from its schema.')));
    return () => cleanups.forEach(f => f());
  }
  const prof = S.profiles[dev.type] || {fields: [{suffix: 'Connected', label: 'Connected'}]};
  const schemas = await loadSchemas(), schema = schemas[dev.name];
  detail.append(h('div', {class: 'panel'}, h('div', {class: 'panel-head'}, h('b', {class: 'big'}, dev.name), h('span', {class: 'muted small'}, `${dev.type} · ${dev.prefix} · ${dev.host || '?'}`),
    rolesOf(dev.name).map(r => h('span', {class: 'pill small'}, r))), schema && schema.description ? h('p', {class: 'doc'}, schema.description) : null));
  const isCam = isCamera(dev.name);
  if (isCam) detail.append(h('div', {class: 'plot-grid'}, cameraPanel(dev, cleanups)));

  // ---- graphs: saved per device on the server, values from the recorded history
  const labels = Object.fromEntries((prof.fields || []).map(f => [f.suffix, f.label || f.suffix]));
  const graphBox = h('div', {class: 'plot-grid'}), graphs = [];
  let saveT = 0;
  const saveView = () => { clearTimeout(saveT); saveT = setTimeout(() => put(`/api/views/device/${dev.name}`, {graphs: graphs.map(x => x.g)}).catch(() => {}), 400); };
  const addGraph = (g, save = true) => {
    const gp = graphPanel(dev, g, labels, {onChange: saveView, onClose: () => { graphs.splice(graphs.indexOf(gp), 1); saveView(); }});
    graphs.push(gp); graphBox.append(gp.el);
    if (save) saveView();
    return gp;
  };
  const newGraph = suffix => addGraph({id: Math.random().toString(36).slice(2, 8), title: labels[suffix] || suffix, pvs: [suffix], window_s: 3600, logY: false});
  cleanups.push(() => graphs.forEach(x => x.destroy()));
  const view = await api(`/api/views/device/${dev.name}`).catch(() => ({graphs: []}));
  if (view.graphs.length) view.graphs.forEach(g => addGraph(g, false));
  else if (prof.trend) addGraph({id: 'main', title: labels[prof.trend] || prof.trend, pvs: [prof.trend], window_s: 3600}, false);
  const graphMenu = (btn, suffix) => popMenu(btn, [
    ...graphs.map(gp => [`➕ add to "${gp.g.title || gp.g.id}"`, () => { gp.add(suffix); gp.el.scrollIntoView({block: 'nearest'}); }, gp.has(suffix)]),
    ['📈 new graph', () => newGraph(suffix).el.scrollIntoView({block: 'nearest'})]]);

  // ---- values
  const tbl = h('table', {class: 'compact'});
  for (const f of prof.fields.filter(f => !f.array)) {
    const pv = dev.prefix + f.suffix;
    const doc = schema && (schema.params || []).find(p => p.name === f.suffix.replace(/_RBV$/, ''));
    const age = h('span', {class: 'muted small'});
    const der = f.derived || (doc && doc.derived);
    const name = h('td', {title: (der ? `derived: ${der.expr}\n` : '') + ((doc && doc.doc) || pv)}, der ? h('span', {class: 'derived-mark', title: 'derived value'}, 'ƒ ') : null, f.label || f.suffix);
    const numeric = f.numeric || typeof f.precision === 'number';
    tbl.append(h('tr', {}, name, h('td', {}, pvCell(pv, {precision: f.precision, onValue: (v, p) => { age.dataset.ts = p.ts || ''; }})), h('td', {}, age),
      h('td', {}, numeric ? h('button', {class: 'icon', title: 'plot: new graph or add to one', onclick: e => graphMenu(e.currentTarget, f.suffix)}, '📈') : null)));
  }
  const ti = setInterval(() => tbl.querySelectorAll('[data-ts]').forEach(a => { a.textContent = a.dataset.ts ? fmtDur(Date.now() / 1000 - a.dataset.ts) + ' ago' : ''; }), 1000);
  cleanups.push(() => clearInterval(ti));
  const ctls = h('div', {class: 'controls'});
  if (prof.controls && prof.controls.length && !S.lab.read_only && can('operator')) prof.controls.forEach(c => ctls.append(controlRow(dev, c)));
  detail.append(h('div', {class: 'two'}, h('div', {class: 'panel'}, h('div', {class: 'panel-head'}, h('b', {}, 'Values')), tbl),
    h('div', {class: 'panel'}, h('div', {class: 'panel-head'}, h('b', {}, 'Controls'), h('span', {class: 'muted small'}, 'no mark = written at once · ⚠ confirm · 🔒 PIN · refused while a plan runs')),
      ctls.firstChild ? ctls : h('p', {class: 'muted small'}, S.lab.read_only ? 'dashboard is read-only' : !can('operator') ? `${S.me.name} is a viewer` : 'none'))));
  if (dev.type === 'derived') detail.append(derivedEditor(dev, schemas));
  detail.append(h('div', {class: 'row'}, h('b', {}, 'Graphs'), h('span', {class: 'muted small'}, 'history is recorded on the server - graphs keep their past across pages and restarts'),
    h('span', {class: 'grow'}), h('button', {class: 'ghost', onclick: () => addGraph({id: Math.random().toString(36).slice(2, 8), title: 'new graph', pvs: [], window_s: 3600})}, '+ Empty graph')),
  graphBox, historyBox(dev));
  if (!isCam) prof.fields.filter(f => f.array).forEach(f => {
    const body = h('div', {class: 'plot-body short'}), p = new Plot(body, {xLabel: 'index'});
    cleanups.push(PV.sub([dev.prefix + f.suffix], v => { if (v.connected && Array.isArray(v.value)) p.setSeries([{name: f.label, x: v.value.map((_, i) => i), y: v.value}]); }), () => p.destroy());
    detail.append(h('div', {class: 'panel'}, h('div', {class: 'panel-head'}, h('b', {}, f.label)), body));
  });
  return () => cleanups.forEach(f => f());
};

// ================================================================== Logbook
Pages.elog = async (root, params) => {
  const cleanups = [];
  if (!S.lab.elog.enabled) { root.append(h('div', {class: 'panel'}, h('p', {}, 'ELOG is not configured: add an elog: section to the lab file.'))); return; }
  const ob = h('div', {class: 'panel'});
  root.append(h('div', {class: 'two'}, h('div', {class: 'panel'}, h('div', {class: 'panel-head'}, h('b', {}, 'New entry'), h('span', {class: 'muted small'}, `notebook ${(S.lab.experiment || {}).notebook || '—'}`)),
    composer({runUid: params.get('run'), rows: 10, onPosted: () => refresh()})), ob));
  async function refresh() {
    const o = await api('/api/elog/outbox').catch(() => null);
    ob.innerHTML = ''; ob.append(h('div', {class: 'panel-head'}, h('b', {}, 'Outbox')));
    if (!o) return;
    const p = o.poster, age = p ? Date.now() / 1000 - p.time : null;
    ob.append(h('p', {class: 'small'}, h('span', {class: 'dot ' + (!p || age > 300 ? 'bad' : p.ok ? 'ok' : 'warn')}),
      !p ? 'ELOG service has never run' : age > 300 ? `ELOG service not running (seen ${fmtDur(age)} ago)` : p.ok ? `ELOG service running → ${p.url}` : `ELOG service retrying → ${p.url}`));
    const table = (title, items, extra) => {
      if (!items.length) return;
      ob.append(h('h4', {}, `${title} (${items.length})`));
      items.forEach(e => ob.append(h('div', {class: 'qitem'}, h('span', {class: 'small muted'}, fmtShort(e.created)), ' ', e.subject,
        h('span', {class: 'small muted'}, ` · ${e.logbook}`), e.error ? h('div', {class: 'small bad-text'}, e.error) : null, h('span', {class: 'qbtn'}, extra(e)))));
    };
    table('Failed', o.failed, e => h('button', {class: 'small', onclick: async () => { await tryApi(() => post('/api/elog/retry', {id: e.id}), 'retry'); refresh(); }}, 'Retry'));
    table('Pending', o.pending, e => e.attempts ? `${e.attempts} tries` : '');
    table('Sent', o.sent.slice(0, 25), e => e.link ? h('a', {href: e.link, target: '_blank'}, '#' + e.msg_id) : '');
  }
  refresh(); const ti = setInterval(refresh, 5000); cleanups.push(() => clearInterval(ti));
  return () => cleanups.forEach(f => f());
};

// ================================================================== System
function alarmsPanel() {
  const box = h('div', {class: 'panel'}), list = h('div');
  let rules = [];
  const num = v => (v === null || v === undefined ? '' : v);
  const draw = status => {
    list.innerHTML = '';
    rules.forEach((r, i) => {
      const st = (status || []).find(x => x.name === r.name) || {};
      const f = (k, ph, w, conv = x => x) => h('input', {value: num(r[k]), placeholder: ph, style: `width:${w}`, onchange: e => { r[k] = conv(e.target.value); }});
      const acts = ['notify', 'elog'].map(a => h('label', {class: 'small'}, h('input', {type: 'checkbox', checked: (r.actions || []).includes(a),
        onchange: e => { r.actions = (r.actions || []).filter(x => x !== a).concat(e.target.checked ? [a] : []); }}), a));
      list.append(h('div', {class: 'derived-def'}, h('div', {class: 'row'},
        h('span', {class: 'dot ' + (st.active ? 'bad' : st.pending_since ? 'warn' : 'ok'), title: st.why || ''}),
        f('name', 'name', '160px'), f('ref', 'device.attr (itc1.temp)', '150px'),
        h('span', {class: 'small muted'}, '>'), f('above', 'above', '70px'), h('span', {class: 'small muted'}, '<'), f('below', 'below', '70px'),
        h('span', {class: 'small muted'}, '|d/dt| >'), f('rate_per_min', '/min', '60px'), h('span', {class: 'small muted'}, 'for'), f('for_s', 's', '50px'),
        acts, h('label', {class: 'small'}, h('input', {type: 'checkbox', checked: r.enabled !== false, onchange: e => { r.enabled = e.target.checked; }}), 'on'),
        h('button', {class: 'icon', onclick: () => { rules.splice(i, 1); draw(status); }}, '✕')),
        h('div', {class: 'row small muted'}, st.value != null ? `now ${fmt(st.value)}${st.why ? ' · ' + st.why : ''}` : 'no value yet', f('note', 'what to do when it fires', '60%'))));
    });
  };
  const load = async () => { const a = await api('/api/alarms').catch(() => ({rules: []})); rules = a.rules.map(r => ({...r})); draw(a.rules); };
  box.append(h('div', {class: 'panel-head'}, h('b', {}, 'Alarms'), h('span', {class: 'muted small'}, 'rules on device values: notification and / or ELOG entry'),
    h('span', {class: 'grow'}), can('operator') ? h('button', {class: 'ghost', onclick: () => { rules.push({name: '', ref: '', for_s: 60, actions: ['notify'], enabled: true}); draw(); }}, '+ Rule') : null,
    can('operator') ? h('button', {class: 'primary', onclick: async () => {
      const keep = ['name', 'ref', 'above', 'below', 'rate_per_min', 'for_s', 'actions', 'enabled', 'logbook', 'note'];
      const r = await tryApi(() => put('/api/alarms', {rules: rules.map(x => Object.fromEntries(keep.map(k => [k, x[k]])))}), 'alarms');
      if (r) { toast('alarm rules saved'); load(); }
    }}, 'Save') : null), list);
  load();
  return box;
}

function auditPanel() {
  const box = h('div', {class: 'panel'}), list = h('div', {class: 'audit'});
  const q = h('input', {placeholder: 'filter (user, PV, path…)', style: 'width:220px'});
  const load = async () => {
    const rows = await api(`/api/audit?n=150&text=${encodeURIComponent(q.value)}`).catch(() => []);
    list.innerHTML = '';
    rows.forEach(r => list.append(h('div', {class: 'qitem small' + (r.ok ? '' : ' bad-text')}, h('span', {class: 'muted'}, fmtShort(r.t)), ' ',
      h('b', {}, r.user), ' ', r.what, ' ', r.detail && Object.keys(r.detail).length ? h('code', {}, JSON.stringify(r.detail).slice(0, 160)) : null)));
  };
  q.addEventListener('input', () => { clearTimeout(q._t); q._t = setTimeout(load, 300); });
  box.append(h('div', {class: 'panel-head'}, h('b', {}, 'Audit log'), h('span', {class: 'muted small'}, 'every change through the dashboard and the AI tools'),
    h('span', {class: 'grow'}), q, h('button', {class: 'icon', onclick: load}, '⟳')), list);
  load();
  return box;
}

function usersPanel() {
  const box = h('div', {class: 'panel'}), tbl = h('table', {class: 'compact'});
  const load = async () => {
    const users = await api('/api/users').catch(() => []);
    tbl.innerHTML = '';
    tbl.append(h('tr', {}, h('th', {}, 'user'), h('th', {}, 'name'), h('th', {}, 'role'), h('th', {})));
    users.forEach(u => {
      const role = h('select', {}, ['viewer', 'operator', 'admin'].map(r => h('option', {selected: r === u.role}, r)));
      role.addEventListener('change', async () => { if (await tryApi(() => put(`/api/users/${u.name}`, {role: role.value}), 'role')) toast(`${u.name}: ${role.value}`); load(); });
      tbl.append(h('tr', {}, h('td', {}, u.name), h('td', {}, u.display), h('td', {}, role), h('td', {},
        h('button', {class: 'small', onclick: () => { const pw = prompt(`New password for ${u.name} (8+ characters)`); if (pw) tryApi(() => put(`/api/users/${u.name}`, {password: pw}), 'password').then(r => r && toast('password set')); }}, 'Password'),
        h('button', {class: 'small danger', onclick: async () => { if (confirm(`Remove ${u.name}?`) && await tryApi(() => del(`/api/users/${u.name}`), 'remove')) load(); }}, 'Remove'))));
    });
  };
  const name = h('input', {placeholder: 'user', style: 'width:110px'}), display = h('input', {placeholder: 'full name', style: 'width:150px'});
  const pw = h('input', {type: 'password', placeholder: 'password', autocomplete: 'new-password', style: 'width:130px'});
  const role = h('select', {}, ['viewer', 'operator', 'admin'].map(r => h('option', {selected: r === 'operator'}, r)));
  box.append(h('div', {class: 'panel-head'}, h('b', {}, 'Users'), h('span', {class: 'muted small'}, 'viewer: sees · operator: runs the lab · admin: + users, settings')), tbl,
    h('div', {class: 'row enter-scope'}, name, display, pw, role, h('button', {class: 'primary', onclick: async () => {
      if (await tryApi(() => put(`/api/users/${encodeURIComponent(name.value.trim())}`, {password: pw.value, role: role.value, display: display.value || null}), 'add user')) {
        name.value = display.value = pw.value = ''; toast('user added'); load();
      }
    }}, 'Add')));
  load();
  return box;
}

Pages.system = async root => {
  await loadDevices();
  const cleanups = [];
  const svc = h('div', {class: 'panel'}), dirs = h('div', {class: 'panel'});
  root.append(h('div', {class: 'two'}, svc, dirs));
  const drawSvc = async () => {
    const s = await api('/api/system').catch(() => null);
    svc.innerHTML = ''; svc.append(h('div', {class: 'panel-head'}, h('b', {}, `Control host ${s ? s.host : ''}`)));
    if (!s) return;
    const t = h('table', {class: 'compact'});
    s.services.forEach(x => t.append(h('tr', {}, h('td', {}, h('span', {class: 'dot ' + (x.up ? 'ok' : 'bad')}), x.name), h('td', {class: 'small muted'}, x.addr))));
    svc.append(t);
    if (s.storage) {
      const mb = b => b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB' : (b / 1e6).toFixed(1) + ' MB';
      const st = h('table', {class: 'compact'});
      s.storage.forEach(x => st.append(h('tr', {}, h('td', {}, x.what, h('div', {class: 'small muted'}, h('code', {}, x.path))),
        h('td', {class: 'small'}, x.exists ? mb(x.bytes) + (x.files != null ? ` · ${x.files} files` : '') + (x.total ? ` of ${mb(x.total)}` : '') : 'not created yet'))));
      svc.append(h('h4', {}, 'Data storage'), st);
    }
    const b = s.backup || {};
    const last = b.last || {};
    svc.append(h('h4', {}, 'Backup'), h('p', {class: 'small'}, !b.target ? 'not configured (lab.yaml backup: {target, every_h, keep})'
      : [h('span', {class: 'dot ' + (last.error || (last.errors || []).length ? 'warn' : last.time && Date.now() / 1000 - last.time < b.every_h * 3600 * 1.5 ? 'ok' : 'bad')}),
        `${b.target} every ${b.every_h} h, keep ${b.keep}: `, last.time ? `last ${fmtShort(last.time)}` : 'never',
        last.snapshot ? ` · ${last.files} files, ${(last.bytes / 1e6).toFixed(1)} MB` : '', last.error ? ` · ${last.error}` : '',
        (last.errors || []).length ? ` · ${last.errors.length} files skipped` : '']));
  };
  drawSvc(); const ti = setInterval(drawSvc, 5000); cleanups.push(() => clearInterval(ti));
  const hist = h('div', {class: 'panel enter-scope'});
  root.append(hist);
  root.append(alarmsPanel());
  if (S.accounts && can('operator')) root.append(auditPanel());
  if (S.accounts && can('admin')) root.append(usersPanel());
  const drawHist = async () => {
    const s = await api('/api/history/settings').catch(() => null);
    hist.innerHTML = ''; hist.append(h('div', {class: 'panel-head'}, h('b', {}, 'Value history'), h('span', {class: 'muted small'},
      s ? `${s.stats.recording} values recorded · ${s.stats.size_mb} MB · ${s.stats.samples} raw rows · ${s.stats.minutes} minute envelopes` : '')));
    if (!s) return;
    const ins = {};
    const f = {period_s: 'sample every (s)', raw_days: 'raw values kept (days)', keep_days: 'minute envelopes kept (days)'};
    Object.entries(f).forEach(([k, l]) => hist.append(h('div', {class: 'ctl'}, h('label', {}, l), ins[k] = h('input', {type: 'number', step: 'any', min: 0, style: 'width:100px', value: s.default[k]}))));
    const over = Object.entries(s.devices);
    hist.append(h('button', {class: 'primary', onclick: async () => {
      const d = {}; for (const k in ins) d[k] = +ins[k].value;
      if (await tryApi(() => put('/api/history/settings', {default: d, devices: s.devices}), 'history settings')) { toast('history settings saved'); drawHist(); }
    }}, 'Save'), h('p', {class: 'small muted'}, over.length ? 'Per-device settings: ' + over.map(([n, v]) => `${n} (${Object.entries(v).map(([k, x]) => `${k} ${x}`).join(', ')})`).join('; ') + ' - change them on the device page.'
      : 'Per-device settings: on each device page (History).'));
  };
  drawHist();
  dirs.append(h('div', {class: 'panel-head'}, h('b', {}, 'Device directories')));
  for (const [prefix, info] of Object.entries(S.directories || {}))
    dirs.append(h('p', {class: 'small'}, h('code', {}, prefix), ` on ${info.Host || '?'} · peers: ${info.Peers || 'none'}`));
  const iocs = h('div', {class: 'panel'}); root.append(iocs);
  iocs.append(h('div', {class: 'panel-head'}, h('b', {}, 'IOCs')));
  const cols = [['Connected', 'Instrument', v => truthy(v) ? '● on' : '○ off'], ['LastError', 'Last error'], ['IOC:HEARTBEAT', 'Heartbeat'], ['IOC:UPTIME', 'Uptime'], ['IOC:IOC_CPU_LOAD', 'CPU %', v => fmt(v, 1)]];
  const byHost = {};
  S.devices.forEach(d => (byHost[d.host || '?'] = byHost[d.host || '?'] || []).push(d));
  for (const [host, devs] of Object.entries(byHost)) {
    const t = h('table', {class: 'compact'}, h('tr', {}, h('th', {}, `🖥 ${host}`), cols.map(c => h('th', {}, c[1])), h('th', {})));
    devs.forEach(d => t.append(d.enabled ? h('tr', {}, h('td', {}, h('a', {href: `#devices?d=${d.name}`}, d.name), h('div', {class: 'small muted'}, d.type)),
      cols.map(([suf, , f]) => h('td', {class: 'small'}, pvCell(d.prefix + suf, f ? {format: f} : {}))),
      h('td', {}, S.lab.read_only ? '' : h('button', {class: 'small', title: 'IOC:SYSRESET - the supervisor restarts it; nothing is written to the instrument',
        onclick: () => putPV(d.prefix + 'IOC:SYSRESET', 1, `Restart IOC ${d.name}`, 'confirm')}, 'Restart')))
      : h('tr', {}, h('td', {}, d.name), h('td', {colspan: cols.length + 1, class: 'muted small'}, 'disabled'))));
    iocs.append(t);
  }
  return () => cleanups.forEach(f => f());
};

// ================================================================== router
const NAV = [['live', '▶', 'Control room'], ['launch', '🧪', 'Experiments'], ['runs', '📊', 'Runs'], ['devices', '🔌', 'Devices'],
  ['elog', '📓', 'Logbook'], ['system', '🖥', 'System']];
let cleanup = null;
async function render() {
  const main = $('#main');
  if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; }
  cleanupPVs(main); destroyComposers(document);
  document.querySelectorAll('.modal-back').forEach(m => m.remove());
  main.innerHTML = '';
  const [page, query] = (location.hash.slice(1) || 'live').split('?');
  const params = new URLSearchParams(query || '');
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('active', a.dataset.page === page));
  try { cleanup = await (Pages[page] || Pages.live)(main, params) || null; } catch (e) { console.error(e); main.append(h('div', {class: 'panel'}, 'Error: ' + e.message)); }
}

(async function init() {
  $('#nav').append(...NAV.map(([k, icon, label]) => h('a', {href: '#' + k, 'data-page': k, title: label}, h('span', {class: 'ico'}, icon), h('span', {class: 'lbl'}, label))));
  const me = await api('/api/me');
  S.accounts = me.accounts; S.me = me.user;
  if (!me.user) { loginScreen(); return; }
  S.lab = await api('/api/lab');
  S.profiles = await api('/api/profiles').catch(() => ({}));
  document.title = `FastQ · ${(S.lab.metadata || {}).facility || S.lab.host}`;
  $('#brand-sub').textContent = (S.lab.metadata || {}).facility || S.lab.host;
  PV.connect();
  await poll(); setInterval(poll, 2000);
  window.addEventListener('hashchange', render);
  render();
})();
