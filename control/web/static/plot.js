// FastQ plots: canvas line / scatter (+ error bars) and heatmaps. No external libraries.
//   const p = new Plot(el, {xLabel, yLabel, xTime, logY});
//   p.setSeries([{name, x, y, err, lo, hi}]);  p.setHeatmap({x, y, z, zLabel});   (lo/hi: min/max band)
// Mouse: drag = zoom box, double click = reset, legend click = hide/show series, hover = values.
'use strict';

const PALETTE = ['#2f6fdf', '#e8590c', '#2b9348', '#c2255c', '#7048e8', '#0c8599', '#e67700', '#5c940d',
  '#d6336c', '#495057'];

function viridis(t) {  // 0..1 -> rgb
  const c = [[68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37]];
  t = Math.max(0, Math.min(1, t)) * (c.length - 1);
  const i = Math.min(c.length - 2, Math.floor(t)), f = t - i;
  return c[i].map((v, k) => Math.round(v + (c[i + 1][k] - v) * f));
}

function niceTicks(lo, hi, n = 6) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return out;
}
function fmtTick(v, span) {
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a >= 1e5 || a < 1e-3) return v.toExponential(span / a < 1e-3 ? 3 : 1);
  const d = Math.max(0, Math.min(6, -Math.floor(Math.log10(span / 5 || 1))));
  return v.toFixed(d);
}

class Plot {
  constructor(el, opts = {}) {
    this.el = el; this.o = Object.assign({xLabel: '', yLabel: '', xTime: null, logY: false, logX: false, title: ''}, opts);
    this.series = []; this.heat = null; this.hidden = new Set(); this.view = null; this.hover = null; this.drag = null;
    this.cv = document.createElement('canvas'); this.cv.className = 'plotcv';
    this.legend = document.createElement('div'); this.legend.className = 'legend';
    el.append(this.cv, this.legend);
    const pos = e => { const r = this.cv.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    this.cv.addEventListener('mousemove', e => { this.hover = pos(e); if (this.drag) this.drag[2] = this.hover; this.draw(); });
    this.cv.addEventListener('mouseleave', () => { this.hover = null; this.draw(); });
    this.cv.addEventListener('mousedown', e => { const p = pos(e); this.drag = [p, p, p]; });
    window.addEventListener('mouseup', this._up = () => {
      if (!this.drag) return;
      const [a, , b] = this.drag; this.drag = null;
      if (Math.abs(a[0] - b[0]) > 6 && Math.abs(a[1] - b[1]) > 6 && this.map) {
        const m = this.map;
        this.view = {x0: m.ix(Math.min(a[0], b[0])), x1: m.ix(Math.max(a[0], b[0])), y0: m.iy(Math.max(a[1], b[1])), y1: m.iy(Math.min(a[1], b[1]))};
      }
      this.draw();
    });
    this.cv.addEventListener('dblclick', () => { this.view = null; this.draw(); });
    this._ro = new ResizeObserver(() => this.draw()); this._ro.observe(this.cv);
  }
  destroy() { this._ro.disconnect(); window.removeEventListener('mouseup', this._up); }
  setSeries(series, opts = {}) { this.heat = null; this.series = series || []; Object.assign(this.o, opts); this.drawLegend(); this.draw(); }
  setHeatmap(heat, opts = {}) { this.series = []; this.heat = heat; Object.assign(this.o, opts); this.legend.innerHTML = ''; this.draw(); }
  drawLegend() {
    this.legend.innerHTML = '';
    if (this.series.length < 2 && !this.o.alwaysLegend) return;
    this.series.forEach((s, i) => {
      const it = document.createElement('span');
      it.className = 'li' + (this.hidden.has(s.name) ? ' off' : '');
      it.innerHTML = `<i style="background:${s.color || PALETTE[i % PALETTE.length]}"></i>`;
      it.append(s.name);
      it.onclick = () => { this.hidden.has(s.name) ? this.hidden.delete(s.name) : this.hidden.add(s.name); this.drawLegend(); this.draw(); };
      this.legend.append(it);
    });
  }
  _isTime() {
    if (this.o.xTime != null) return this.o.xTime;
    const s = this.series.find(s => s.x && s.x.length);
    return !!(s && s.x.find(v => v != null) > 1e9);
  }
  draw() {
    const c = this.cv, dpr = window.devicePixelRatio || 1, W = c.clientWidth, H = c.clientHeight;
    if (!W || !H) return;
    c.width = W * dpr; c.height = H * dpr;
    const g = c.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const css = getComputedStyle(document.documentElement);
    const fg = css.getPropertyValue('--fg').trim() || '#222', grid = css.getPropertyValue('--grid').trim() || '#e5e5e5';
    g.fillStyle = css.getPropertyValue('--panel').trim() || '#fff'; g.fillRect(0, 0, W, H);
    g.font = '11px system-ui, sans-serif';
    const L = 64, R = this.heat ? 70 : 12, T = 10, B = 34;
    const pw = W - L - R, ph = H - T - B;
    if (pw < 20 || ph < 20) return;
    const tx = this.o.logX ? v => (v > 0 ? Math.log10(v) : NaN) : v => v;
    const ty = this.o.logY ? v => (v > 0 ? Math.log10(v) : NaN) : v => v;
    // ranges
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    const ext = (x, y) => { if (isFinite(x)) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); } if (isFinite(y)) { y0 = Math.min(y0, y); y1 = Math.max(y1, y); } };
    if (this.heat) {
      this.heat.x.forEach(v => ext(tx(v), NaN)); this.heat.y.forEach(v => ext(NaN, ty(v)));
    } else for (const s of this.series) {
      if (this.hidden.has(s.name)) continue;
      for (let i = 0; i < s.x.length; i++) {
        const y = s.y[i]; if (y == null) continue;
        const e = s.err ? s.err[i] || 0 : 0;
        ext(tx(s.x[i]), ty(y - e)); ext(NaN, ty(y + e));
        if (s.lo && s.lo[i] != null) ext(NaN, ty(s.lo[i]));
        if (s.hi && s.hi[i] != null) ext(NaN, ty(s.hi[i]));
      }
    }
    g.fillStyle = fg;
    if (!isFinite(x0) || !isFinite(y0)) { g.fillText(this.o.empty || 'no data yet', W / 2 - 30, H / 2); this.map = null; return; }
    if (this.view) ({x0, x1, y0, y1} = this.view);
    if (x0 === x1) { x0 -= 0.5; x1 += 0.5; }
    if (y0 === y1) { const d = Math.abs(y0) * 0.01 || 1; y0 -= d; y1 += d; }
    if (!this.view) { const py = (y1 - y0) * 0.05; y0 -= py; y1 += py; }
    const sx = v => L + (v - x0) / (x1 - x0) * pw, sy = v => T + ph - (v - y0) / (y1 - y0) * ph;
    this.map = {ix: p => x0 + (p - L) / pw * (x1 - x0), iy: p => y0 + (T + ph - p) / ph * (y1 - y0)};
    // grid + ticks
    g.strokeStyle = grid; g.lineWidth = 1; g.textAlign = 'center';
    const isTime = this._isTime();
    for (const v of niceTicks(x0, x1, Math.max(2, Math.floor(pw / 90)))) {
      const X = sx(v); g.beginPath(); g.moveTo(X, T); g.lineTo(X, T + ph); g.stroke();
      const lab = isTime ? (x1 - x0 > 86400 ? new Date(v * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : new Date(v * 1000).toLocaleTimeString()) : this.o.logX ? '1e' + fmtTick(v, x1 - x0) : fmtTick(v, x1 - x0);
      g.fillText(lab, X, T + ph + 14);
    }
    g.textAlign = 'right';
    for (const v of niceTicks(y0, y1, Math.max(2, Math.floor(ph / 40)))) {
      const Y = sy(v); g.beginPath(); g.moveTo(L, Y); g.lineTo(L + pw, Y); g.stroke();
      g.fillText(this.o.logY ? '1e' + fmtTick(v, y1 - y0) : fmtTick(v, y1 - y0), L - 4, Y + 4);
    }
    g.textAlign = 'center';
    if (this.o.xLabel && !isTime) g.fillText(this.o.xLabel, L + pw / 2, H - 4);
    if (this.o.yLabel) { g.save(); g.translate(11, T + ph / 2); g.rotate(-Math.PI / 2); g.fillText(this.o.yLabel.slice(0, 60), 0, 0); g.restore(); }
    g.save(); g.beginPath(); g.rect(L, T, pw, ph); g.clip();
    if (this.heat) this._drawHeat(g, sx, sy, tx, ty, L, T, pw, ph, W, fg);
    else this._drawSeries(g, sx, sy, tx, ty);
    g.restore();
    g.strokeStyle = fg; g.strokeRect(L, T, pw, ph);
    if (this.heat) this._colorbar(g, L + pw + 12, T, ph, fg);
    // zoom box / hover readout
    if (this.drag) {
      const [a, , b] = this.drag; g.strokeStyle = '#2f6fdf'; g.setLineDash([4, 3]);
      g.strokeRect(a[0], a[1], b[0] - a[0], b[1] - a[1]); g.setLineDash([]);
    } else if (this.hover && !this.heat) this._readout(g, sx, sy, tx, ty, fg, W);
    else if (this.hover && this.heat) this._heatReadout(g, fg, W);
  }
  _drawSeries(g, sx, sy, tx, ty) {
    this.series.forEach((s, i) => {
      if (this.hidden.has(s.name)) return;
      const col = s.color || PALETTE[i % PALETTE.length];
      g.strokeStyle = col; g.fillStyle = col; g.lineWidth = 1.5;
      if (s.lo && s.hi) {  // envelope (e.g. min/max of each history bucket)
        g.globalAlpha = 0.16; g.beginPath(); let n = 0;
        for (let k = 0; k < s.x.length; k++) { if (s.hi[k] == null) continue; const X = sx(tx(s.x[k])), Y = sy(ty(s.hi[k])); n++ ? g.lineTo(X, Y) : g.moveTo(X, Y); }
        for (let k = s.x.length - 1; k >= 0; k--) { if (s.lo[k] == null) continue; g.lineTo(sx(tx(s.x[k])), sy(ty(s.lo[k]))); }
        if (n) g.fill();
        g.globalAlpha = 1;
      }
      const mode = s.mode || this.o.mode || (s.x.length > 300 ? 'line' : 'both');
      if (s.err) {
        g.globalAlpha = 0.45;
        for (let k = 0; k < s.x.length; k++) {
          const y = s.y[k], e = s.err[k]; if (y == null || !e) continue;
          const X = sx(tx(s.x[k])); g.beginPath(); g.moveTo(X, sy(ty(y - e))); g.lineTo(X, sy(ty(y + e))); g.stroke();
        }
        g.globalAlpha = 1;
      }
      if (mode !== 'markers') {
        g.beginPath(); let pen = false;
        for (let k = 0; k < s.x.length; k++) {
          const X = sx(tx(s.x[k])), y = s.y[k];
          if (y == null || !isFinite(X)) { pen = false; continue; }
          const Y = sy(ty(y)); if (!isFinite(Y)) { pen = false; continue; }
          pen ? g.lineTo(X, Y) : g.moveTo(X, Y); pen = true;
        }
        g.stroke();
      }
      if (mode !== 'line') for (let k = 0; k < s.x.length; k++) {
        const y = s.y[k]; if (y == null) continue;
        g.beginPath(); g.arc(sx(tx(s.x[k])), sy(ty(y)), 2.6, 0, 2 * Math.PI); g.fill();
      }
    });
  }
  _readout(g, sx, sy, tx, ty, fg, W) {
    const [hx, hy] = this.hover; let best = null;
    this.series.forEach((s, i) => {
      if (this.hidden.has(s.name)) return;
      for (let k = 0; k < s.x.length; k++) {
        if (s.y[k] == null) continue;
        const d = Math.hypot(sx(tx(s.x[k])) - hx, sy(ty(s.y[k])) - hy);
        if (!best || d < best.d) best = {d, s, k, i};
      }
    });
    if (!best || best.d > 40) return;
    const {s, k} = best, X = sx(tx(s.x[k])), Y = sy(ty(s.y[k]));
    g.strokeStyle = fg; g.beginPath(); g.arc(X, Y, 5, 0, 2 * Math.PI); g.stroke();
    const xs = this._isTime() ? new Date(s.x[k] * 1000).toLocaleTimeString() : (+s.x[k].toPrecision(6));
    const txt = `${s.name}: x=${xs}  y=${+s.y[k].toPrecision(6)}${s.err && s.err[k] ? ' ± ' + (+s.err[k].toPrecision(2)) : ''}`;
    const w = g.measureText(txt).width + 10, bx = Math.min(W - w - 4, X + 8), by = Math.max(14, Y - 22);
    g.fillStyle = 'rgba(30,30,30,.85)'; g.fillRect(bx, by, w, 18); g.fillStyle = '#fff'; g.textAlign = 'left'; g.fillText(txt, bx + 5, by + 13);
  }
  _drawHeat(g, sx, sy, tx, ty) {
    const {x, y, z} = this.heat;
    let lo = Infinity, hi = -Infinity;
    z.forEach(r => r.forEach(v => { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
    this.zr = [lo, hi];
    const edges = a => a.map((v, i) => [i === 0 ? v - ((a[1] ?? v + 1) - v) / 2 : (a[i - 1] + v) / 2, i === a.length - 1 ? v + (v - (a[i - 1] ?? v - 1)) / 2 : (v + a[i + 1]) / 2]);
    const ex = edges(x.map(tx)), ey = edges(y.map(ty));
    for (let j = 0; j < y.length; j++) for (let i = 0; i < x.length; i++) {
      const v = z[j][i]; if (v == null) continue;
      const [r, gg, b] = viridis(hi > lo ? (v - lo) / (hi - lo) : 0.5);
      g.fillStyle = `rgb(${r},${gg},${b})`;
      const X0 = sx(ex[i][0]), X1 = sx(ex[i][1]), Y0 = sy(ey[j][1]), Y1 = sy(ey[j][0]);
      g.fillRect(X0, Y0, X1 - X0 + 0.5, Y1 - Y0 + 0.5);
    }
  }
  _colorbar(g, X, T, ph, fg) {
    if (!this.zr) return;
    for (let k = 0; k < ph; k++) { const [r, gg, b] = viridis(1 - k / ph); g.fillStyle = `rgb(${r},${gg},${b})`; g.fillRect(X, T + k, 12, 1); }
    g.fillStyle = fg; g.textAlign = 'left';
    const [lo, hi] = this.zr;
    g.fillText(fmtTick(hi, hi - lo), X + 15, T + 8); g.fillText(fmtTick(lo, hi - lo), X + 15, T + ph);
    if (this.heat.zLabel) { g.save(); g.translate(X + 50, T + ph / 2); g.rotate(-Math.PI / 2); g.textAlign = 'center'; g.fillText(this.heat.zLabel.slice(0, 40), 0, 0); g.restore(); }
  }
  _heatReadout(g, fg, W) {
    const m = this.map; if (!m) return;
    const [hx, hy] = this.hover, xv = m.ix(hx), yv = m.iy(hy);
    const {x, y, z} = this.heat;
    const near = (a, v) => a.reduce((b, w, i) => Math.abs(w - v) < Math.abs(a[b] - v) ? i : b, 0);
    const i = near(x, xv), j = near(y, yv), v = z[j] && z[j][i];
    const txt = `x=${+x[i].toPrecision(5)} y=${+y[j].toPrecision(5)} z=${v == null ? '—' : +v.toPrecision(5)}`;
    const w = g.measureText(txt).width + 10;
    g.fillStyle = 'rgba(30,30,30,.85)'; g.fillRect(Math.min(W - w - 4, hx + 8), hy - 22, w, 18);
    g.fillStyle = '#fff'; g.textAlign = 'left'; g.fillText(txt, Math.min(W - w - 4, hx + 8) + 5, hy - 9);
  }
  toBlob() { return new Promise(res => this.cv.toBlob(res, 'image/png')); }
  toCSV() {
    if (this.heat) {
      const {x, y, z} = this.heat;
      return ['y\\x,' + x.join(','), ...y.map((v, j) => v + ',' + z[j].map(q => q ?? '').join(','))].join('\n');
    }
    const rows = ['series,x,y,err'];
    this.series.forEach(s => s.x.forEach((x, k) => rows.push(`"${s.name}",${x},${s.y[k] ?? ''},${s.err ? s.err[k] ?? '' : ''}`)));
    return rows.join('\n');
  }
}
window.Plot = Plot; window.PALETTE = PALETTE; window.viridis = viridis;
