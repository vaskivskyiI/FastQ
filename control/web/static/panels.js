// Panels shared by the pages: run-data plots (spec driven), plot grids with per-plan layouts,
// live camera view, stored camera frames, PV trends.
'use strict';

// ------------------------------------------------------------------ plot spec editor
function specEditor(spec, columnsOf, streams, onApply) {
  const s = JSON.parse(JSON.stringify(spec));
  const f = h('div', {class: 'spec-editor'});
  const sel = (opts, value, extra = {}) => { const e = h('select', extra, opts.map(o => Array.isArray(o) ? h('option', {value: o[0]}, o[1]) : h('option', {}, o))); e.value = value ?? ''; return e; };
  const title = h('input', {value: s.title || '', placeholder: 'title'});
  const stream = sel(streams, s.stream || 'primary');
  const kind = sel([['line', 'line'], ['scatter', 'points'], ['heatmap', 'map (x, y → z)']], s.kind || 'line');
  const x = h('select'), group = h('select'), z = h('select');
  const yFilter = h('input', {placeholder: 'filter columns, or a pattern like cammain_over_*'});
  const yBox = h('div', {class: 'ybox'});
  const avg = h('input', {type: 'checkbox', checked: !!s.average});
  const combine = sel([['', '—'], ['diff', 'difference (hi − lo)'], ['ratio', 'ratio (hi / lo)'], ['rel_diff', 'relative (hi − lo) / lo']], s.combine || '');
  const logY = h('input', {type: 'checkbox', checked: !!s.logY});
  const last = h('input', {type: 'number', value: s.last || '', placeholder: 'all', style: 'width:80px'});
  let ys = new Set(s.y || []);
  const fill = () => {
    const cols = columnsOf(stream.value);
    const opt = (e, v, none) => { e.innerHTML = ''; if (none) e.append(h('option', {value: ''}, none)); cols.forEach(c => e.append(h('option', {}, c))); e.value = v || ''; };
    opt(x, s.x || (cols.includes('time') ? 'time' : cols[0])); opt(group, s.group, '— none —'); opt(z, s.z, '—');
    drawY();
  };
  const drawY = () => {
    const q = yFilter.value.trim().toLowerCase(), cols = columnsOf(stream.value);
    yBox.innerHTML = '';
    const pattern = /[*?[]/.test(q);
    if (pattern) { yBox.append(h('label', {}, h('input', {type: 'checkbox', checked: ys.has(yFilter.value.trim()), onchange: e => { e.target.checked ? ys.add(yFilter.value.trim()) : ys.delete(yFilter.value.trim()); }}), ` pattern ${yFilter.value.trim()}`)); }
    [...ys].filter(v => /[*?[]/.test(v)).forEach(v => yBox.append(h('label', {}, h('input', {type: 'checkbox', checked: true, onchange: e => { e.target.checked ? ys.add(v) : ys.delete(v); }}), ' ' + v)));
    cols.filter(c => !q || pattern || c.toLowerCase().includes(q)).slice(0, 200).forEach(c =>
      yBox.append(h('label', {}, h('input', {type: 'checkbox', checked: ys.has(c), onchange: e => { e.target.checked ? ys.add(c) : ys.delete(c); }}), ' ' + c)));
  };
  yFilter.addEventListener('input', drawY); stream.addEventListener('change', () => { s.x = s.group = s.z = null; fill(); });
  fill();
  const row = (label, ...el) => h('div', {class: 'erow'}, h('span', {}, label), ...el);
  f.append(row('Title', title), row('Data', stream, kind), row('X', x), row('Y', h('div', {class: 'grow'}, yFilter, yBox)),
    row('Curves per', group, combine), row('', h('label', {}, avg, ' average equal x (repeats → mean ± s.e.)'), h('label', {}, logY, ' log y')),
    row('Map z', z), row('Last rows', last),
    h('div', {class: 'row end'}, h('button', {class: 'ghost', onclick: () => onApply(null)}, 'Cancel'),
      h('button', {class: 'primary', onclick: () => {
        const out = {title: title.value.trim() || undefined, stream: stream.value, kind: kind.value, x: x.value, y: [...ys],
          group: group.value || undefined, average: avg.checked || undefined, combine: combine.value || undefined,
          z: kind.value === 'heatmap' ? z.value : undefined, logY: logY.checked || undefined, last: +last.value || undefined};
        Object.keys(out).forEach(k => out[k] === undefined && delete out[k]);
        onApply(out);
      }}, 'Apply')));
  return f;
}

// ------------------------------------------------------------------ one plot of run data
class PlotPanel {
  // source: {uid(): run uid or null, columns(stream): [names], streams(): [names], title(): string}
  constructor(spec, source, {onChange, onRemove} = {}) {
    this.spec = spec; this.src = source; this.onChange = onChange; this.onRemove = onRemove;
    this.info = h('span', {class: 'muted small'});
    this.titleEl = h('b', {}, spec.title || this.autoTitle());
    this.body = h('div', {class: 'plot-body'});
    this.editBox = h('div');
    this.el = h('div', {class: 'panel' + (spec.small ? '' : ' wide-ok')},
      h('div', {class: 'panel-head'}, this.titleEl, this.info, h('span', {class: 'grow'}),
        h('button', {class: 'icon', title: 'edit: columns, curves, averaging…', onclick: () => this.edit()}, '✎'),
        h('button', {class: 'icon', title: 'wider / narrower', onclick: () => this.el.classList.toggle('span2')}, '⤢'),
        h('button', {class: 'icon', title: 'CSV of the plotted curves', onclick: () => this.csv()}, '⬇'),
        h('button', {class: 'icon', title: 'image → new ELOG entry', onclick: () => this.toElog()}, '📓'),
        onRemove ? h('button', {class: 'icon', title: 'remove this plot', onclick: () => onRemove(this)}, '✕') : null),
      this.editBox, this.body);
    this.plot = new Plot(this.body, {logY: !!spec.logY, empty: 'loading…'});
  }
  autoTitle() { const s = this.spec; return `${(s.y || []).join(', ') || '?'} vs ${s.x || 'time'}`; }
  edit() {
    if (this.editBox.firstChild) { this.editBox.innerHTML = ''; return; }
    this.editBox.append(specEditor(this.spec, st => this.src.columns(st), this.src.streams(), out => {
      this.editBox.innerHTML = '';
      if (!out) return;
      this.spec = out; this.titleEl.textContent = out.title || this.autoTitle(); this.plot.o.logY = !!out.logY;
      this.onChange && this.onChange(this); this.refresh(true);
    }));
  }
  async refresh(force = false) {
    const uid = this.src.uid();
    if (!uid || (this.busy && !force)) return;
    this.busy = true;
    try {
      const d = await post(`/api/runs/${uid}/plot`, this.spec);
      this.plot.o.empty = 'no data yet';
      if (d.missing) { this.plot.o.empty = `no "${d.missing}" data in this run`; this.plot.setSeries([]); }
      else if (d.kind === 'heatmap') this.plot.setHeatmap({x: d.x || [], y: d.y || [], z: d.z || [], zLabel: d.z_label}, {xLabel: d.x_label, yLabel: d.y_label});
      else this.plot.setSeries(d.series, {xLabel: d.x_label, yLabel: d.y_label, mode: this.spec.kind === 'scatter' ? 'markers' : null});
      this.info.textContent = d.n ? `${d.n} rows` : '';
    } catch (e) { this.info.textContent = e.message.slice(0, 80); }
    this.busy = false;
  }
  csv() {
    const blob = new Blob([this.plot.toCSV()], {type: 'text/csv'});
    const a = h('a', {href: URL.createObjectURL(blob), download: `${(this.spec.title || 'plot').replace(/\W+/g, '_')}.csv`}); a.click();
  }
  async toElog() {
    S.pendingAttachments.push({blob: await this.plot.toBlob(), name: `${this.src.title()}_${(this.spec.title || 'plot').replace(/\W+/g, '_')}.png`});
    const comp = composer({runUid: this.src.uid()});
    modal('New ELOG entry', comp);
  }
  destroy() { this.plot.destroy(); }
}

// ------------------------------------------------------------------ a grid of plots with a per-plan layout
class PlotGrid {
  // ctx: {uid(), plan(), detail() -> run detail (streams with data_keys), title()}
  constructor(ctx) {
    this.ctx = ctx; this.panels = []; this.el = h('div', {class: 'plot-grid'});
    this.bar = h('div', {class: 'row small'},
      h('button', {class: 'ghost', onclick: () => this.add({title: 'New plot', stream: 'primary', x: 'time', y: []}, true)}, '+ Plot'),
      h('button', {class: 'ghost', title: 'use this set of plots for every run of this plan (all users)', onclick: () => this.save()}, '💾 Save layout for this plan'),
      h('button', {class: 'ghost', title: 'back to the plots the plan suggests', onclick: () => this.reset()}, '↺ Default plots'));
    this.root = h('div', {}, this.bar, this.el);
  }
  source() {
    const d = () => this.ctx.detail() || {streams: {}};
    return {uid: () => this.ctx.uid(), title: () => this.ctx.title(), streams: () => Object.keys(d().streams || {}).length ? Object.keys(d().streams) : ['primary'],
      columns: st => { const dk = (d().streams || {})[st] || {}; return ['time', 'seq_num', ...Object.keys(dk).filter(k => ((dk[k] || {}).shape || []).length <= 1).sort()]; }};
  }
  set(specs) {
    this.panels.forEach(p => p.destroy()); this.panels = []; this.el.innerHTML = '';
    (specs || []).forEach(s => this.add(s));
    this.refresh();
  }
  add(spec, editNow = false) {
    const p = new PlotPanel(spec, this.source(), {onRemove: pp => { pp.destroy(); pp.el.remove(); this.panels = this.panels.filter(x => x !== pp); }});
    this.panels.push(p); this.el.append(p.el);
    if (editNow) p.edit(); else p.refresh();
    return p;
  }
  specs() { return this.panels.map(p => p.spec); }
  async save() {
    const plan = this.ctx.plan(); if (!plan) return;
    if (await tryApi(() => put(`/api/layouts/${encodeURIComponent(plan)}`, this.specs()), 'save layout')) toast(`layout saved for ${plan}`);
  }
  async reset() {
    const plan = this.ctx.plan();
    if (plan) await tryApi(() => del(`/api/layouts/${encodeURIComponent(plan)}`), 'reset layout');
    const d = this.ctx.detail();
    this.set((d && d.default_plots) || []);
  }
  async refresh() { for (const p of this.panels) await p.refresh(); }
  destroy() { this.panels.forEach(p => p.destroy()); }
}

// ------------------------------------------------------------------ camera: live image + spectrum (PVs)
function cameraPanel(dev, cleanups) {
  const P = dev.prefix;
  const img = h('div', {class: 'plot-body'}), spec = h('div', {class: 'plot-body short'});
  const ip = new Plot(img, {xLabel: 'x (px)', yLabel: 'y (px)'}), sp = new Plot(spec, {xLabel: 'pixel', yLabel: 'counts'});
  const live = h('input', {type: 'checkbox'});
  const liveCell = pvCell(P + 'LiveView', {onValue: v => { live.checked = truthy(v); }, format: v => truthy(v) ? 'live' : 'idle'});
  live.addEventListener('change', async () => { if (!await putPV(P + 'LiveView', live.checked ? 1 : 0, `${dev.name} live view`, controlSafety(dev, 'LiveView'))) live.checked = !live.checked; });
  const el = h('div', {class: 'panel span2'}, h('div', {class: 'panel-head'}, h('b', {}, `📷 ${dev.name}`),
    h('span', {class: 'muted small'}, 'exposure ', pvCell(P + 'AcquireTime_RBV', {precision: 3}), ' s · frame ', pvCell(P + 'ArrayCounter'), ' · ', liveCell),
    h('span', {class: 'grow'}), h('label', {class: 'small', title: 'continuous frames for alignment; nothing is saved'}, live, ' Live view'),
    h('button', {class: 'ghost small', onclick: () => putPV(P + 'Acquire', 1, `${dev.name}: take one frame`, controlSafety(dev, 'Acquire'))}, 'Snap')),
    h('div', {class: 'cam'}, img, spec));
  let width = 1;
  const un1 = PV.sub([P + 'LiveWidth'], p => { if (p.connected) width = p.value || 1; });
  const un2 = PV.sub([P + 'Spectrum'], p => { if (p.connected && Array.isArray(p.value)) sp.setSeries([{name: 'spectrum', x: p.value.map((_, i) => i), y: p.value}], {mode: 'line'}); });
  let lastCount = -1;
  const un3 = PV.sub([P + 'ArrayCounter'], async p => {
    if (!p.connected || p.value === lastCount || !el.isConnected) return;
    lastCount = p.value;
    const data = await api(`/api/pv/array?name=${encodeURIComponent(P + 'LiveImage')}`).catch(() => null);
    if (!data || !width) return;
    const rows = Math.max(1, Math.floor(data.length / width)), z = [];
    for (let j = 0; j < rows; j++) z.push(data.slice(j * width, (j + 1) * width));
    ip.setHeatmap({x: [...Array(width).keys()], y: [...Array(rows).keys()], z, zLabel: 'counts'});
  });
  cleanups.push(() => { un1(); un2(); un3(); ip.destroy(); sp.destroy(); });
  return el;
}

// ------------------------------------------------------------------ stored camera frames of a run
function framePanel(uid, key, cleanups) {
  const img = h('div', {class: 'plot-body'}), spec = h('div', {class: 'plot-body short'});
  const ip = new Plot(img, {xLabel: 'x (px)', yLabel: 'y (px)'}), sp = new Plot(spec, {xLabel: 'pixel', yLabel: 'counts'});
  const slider = h('input', {type: 'range', min: 0, max: 0, value: 0, style: 'flex:1'});
  const lab = h('span', {class: 'small muted'});
  const load = async i => {
    const d = await tryApi(() => api(`/api/runs/${uid}/frame?key=${encodeURIComponent(key)}&index=${i}`), 'frame');
    if (!d) return;
    slider.max = d.count - 1; slider.value = d.index; lab.textContent = `frame ${d.index + 1} / ${d.count} · ${d.shape.join(' × ')}${d.step > 1 ? ` (every ${d.step}th px shown)` : ''}`;
    const rows = Math.max(1, Math.floor(d.data.length / d.width)), z = [];
    for (let j = 0; j < rows; j++) z.push(d.data.slice(j * d.width, (j + 1) * d.width));
    ip.setHeatmap({x: [...Array(d.width).keys()].map(v => v * d.step), y: [...Array(rows).keys()].map(v => v * (d.step_y || d.step)), z, zLabel: 'counts'});
    sp.setSeries([{name: 'spectrum', x: d.spectrum.map((_, i) => i), y: d.spectrum}], {mode: 'line'});
  };
  slider.addEventListener('change', () => load(+slider.value));
  load(-1);
  cleanups.push(() => { ip.destroy(); sp.destroy(); });
  return h('div', {class: 'panel span2'}, h('div', {class: 'panel-head'}, h('b', {}, `🖼 ${key}`), lab),
    h('div', {class: 'row'}, h('button', {class: 'icon', onclick: () => load(Math.max(0, +slider.value - 1))}, '◀'), slider,
      h('button', {class: 'icon', onclick: () => load(+slider.value + 1)}, '▶')), h('div', {class: 'cam'}, img, spec));
}

// ------------------------------------------------------------------ device graphs (history + live)
const WINDOWS = [['5 min', 300], ['30 min', 1800], ['1 h', 3600], ['2 h', 7200], ['12 h', 43200], ['1 day', 86400], ['7 days', 604800], ['30 days', 2592000], ['1 year', 31536000]];
// One graph of a device page: several values on one axis, from the recorded history + live updates.
// g = {id, title, pvs: [suffix], window_s, logY}; onChange(g) after every edit, onClose() on ✕.
function graphPanel(dev, g, labels, {onChange, onClose}) {
  const trends = new Map(), body = h('div', {class: 'plot-body short'}), chips = h('div', {class: 'chips'});
  const p = new Plot(body, {xTime: true, logY: !!g.logY, alwaysLegend: true});
  let raf = 0;
  const draw = () => { if (raf) return; raf = requestAnimationFrame(() => { raf = 0;
    p.setSeries(g.pvs.filter(s => trends.has(s)).map(s => { const t = trends.get(s); return {name: labels[s] || s, x: t.x, y: t.y, lo: t.lo, hi: t.hi}; }), {mode: 'line'});
    src.textContent = [...trends.values()].some(t => t.source === 'minutes') ? 'min/max envelope (minute history)' : ''; }); };
  const src = h('span', {class: 'small muted'});
  const drawChips = () => { chips.innerHTML = ''; g.pvs.forEach(s => chips.append(h('span', {class: 'chip small'}, labels[s] || s,
    h('button', {class: 'icon', title: 'remove from this graph', onclick: () => remove(s)}, '×')))); };
  const add = s => { if (g.pvs.includes(s)) return false; g.pvs.push(s); attach(s); drawChips(); onChange(g); return true; };
  const attach = s => { const t = new Trend(dev.prefix + s, labels[s] || s, g.window_s).start(); t.listeners.push(draw); trends.set(s, t); };
  const remove = s => { g.pvs = g.pvs.filter(x => x !== s); const t = trends.get(s); if (t) t.stop(); trends.delete(s); drawChips(); draw(); onChange(g); };
  g.pvs.forEach(attach); drawChips();
  const title = h('input', {class: 'title-edit', value: g.title || '', placeholder: 'graph title', onchange: () => { g.title = title.value; onChange(g); }});
  const win = h('select', {onchange: () => { g.window_s = +win.value; trends.forEach(t => t.setWindow(g.window_s)); p.view = null; onChange(g); }},
    WINDOWS.map(([l, v]) => h('option', {value: v, selected: v === +g.window_s}, l)));
  if (!WINDOWS.some(([, v]) => v === +g.window_s)) win.append(h('option', {value: g.window_s, selected: true}, fmtDur(g.window_s)));
  const log = h('input', {type: 'checkbox', checked: !!g.logY, onchange: () => { g.logY = log.checked; p.o.logY = g.logY; p.draw(); onChange(g); }});
  const el = h('div', {class: 'panel graph', 'data-graph': g.id}, h('div', {class: 'panel-head'}, title, h('span', {class: 'grow'}), src,
    win, h('label', {class: 'small'}, log, 'log'),
    h('button', {class: 'icon', title: 'CSV', onclick: () => download(`${dev.name}_${g.title || g.id}.csv`, p.toCSV(), 'text/csv')}, '⬇'),
    h('button', {class: 'icon', title: 'close graph', onclick: () => { destroy(); el.remove(); onClose(); }}, '✕')), chips, body);
  const destroy = () => { trends.forEach(t => t.stop()); trends.clear(); p.destroy(); };
  return {el, g, add, destroy, has: s => g.pvs.includes(s)};
}

