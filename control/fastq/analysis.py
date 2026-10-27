"""Run data in one place - for the dashboards, the AI tools and notebooks.

    from fastq.analysis import load_run
    run = load_run(-1)                       # latest; or a scan_id (int), a uid / uid prefix
    run.md["comment"], run.streams
    df = run.table()                         # primary stream, scalar columns (pandas)
    frames = run.frames("cammain_image")     # camera frames (from the detector's HDF5 file, via Tiled)
    run.plot({"x": "delay", "y": ["cammain_over_camref_HE"], "group": "pump_shutter", "average": True})

Plot spec (dict; default_plots builds them from the plan's md["plot_hints"]; a run may carry its own md["plots"],
the dashboards save edited ones per plan):
  title, stream ("primary"), kind "line" | "scatter" | "heatmap"
  x, y: [columns]           y may contain glob patterns ("cammain_over_camref_*")
  group: column              one series per value (e.g. pump shutter 0 / 1)
  average: true              mean +- standard error of all points with the same x (and group):
                             repeats of a scan collapse into one curve
  combine: diff | ratio | rel_diff   with a 2-valued group: hi - lo, hi / lo, (hi - lo) / lo
                             (pump-probe: pumped - unpumped)
  z: column (heatmap)        mean of z on the (x, y[0]) grid
  last: N                    only the last N rows (time traces)
"""
from __future__ import annotations

import fnmatch
import math
from pathlib import Path

import numpy as np

TIME_COLS = ("time",)


# ------------------------------------------------------------------ access
def tiled_client(lab: dict | None = None):
    from tiled.client import from_uri

    if lab is None:
        from fastq.session import load_lab

        lab = load_lab()
    uri = (lab.get("data") or {}).get("tiled_uri") or "http://127.0.0.1:8000"
    from fastq.session import tiled_api_key

    return from_uri(uri, api_key=tiled_api_key(lab))


def find_run(client, ref):
    """(uid, run node) for 'latest' / -1 / -n (n-th latest), a scan_id (int or digits) or a uid (prefix)."""
    if ref in ("latest", None):
        ref = -1
    if isinstance(ref, str) and ref.lstrip("-").isdigit() and len(ref) < 9:
        ref = int(ref)
    if isinstance(ref, int):
        if ref < 0:
            uid = client.keys()[ref]
            return uid, client[uid]
        from tiled.queries import Key

        hits = client.search(Key("start.scan_id") == ref)
        if not len(hits):
            raise KeyError(f"no run with scan_id {ref}")
        uid = hits.keys()[-1]
        return uid, hits[uid]
    if ref in client:
        return ref, client[ref]
    for uid in client.keys()[-1:-301:-1]:  # a tiled page holds at most 300
        if uid.startswith(ref):
            return uid, client[uid]
    raise KeyError(f"no run {ref!r}")


def _raw_keys(run, stream: str) -> dict:
    return dict((run[stream].metadata or {}).get("data_keys") or {})


def _config_values(run) -> dict:
    """Configuration readings of every stream (kind=config signals, e.g. a lock-in amplitude)."""
    out = {}
    for s in run:
        for obj in ((run[s].metadata or {}).get("configuration") or {}).values():
            for k, v in ((obj or {}).get("data") or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out.setdefault(k, float(v))
    return out


def _derived(run) -> list[dict]:
    return list(((run.metadata or {}).get("start") or {}).get("derived") or [])


def data_keys(run, stream: str = "primary") -> dict:
    """{column: {shape, dtype, units, source, ...}} - from the stream metadata, plus the run's derived
    values (fastq.derived, source "derived: <formula>") in every stream that holds one of their inputs."""
    dk = _raw_keys(run, stream)
    everything = None
    for d in _derived(run):
        keys = list((d.get("keys") or {}).values())
        if not keys or not any(k in dk for k in keys):
            continue
        if everything is None:
            everything = {k for s in run for k in _raw_keys(run, s)} | set(_config_values(run))
        if all(k in everything for k in keys):
            dk[d["name"]] = {"shape": [], "dtype": "number", "units": d.get("units"),
                             "source": "derived: " + d["expr"], "derived": d}
    return dk


def summary(uid: str, run) -> dict:
    md = run.metadata
    start, stop = md.get("start") or {}, md.get("stop") or {}
    return {"uid": uid, "scan_id": start.get("scan_id"), "plan_name": start.get("plan_name"),
            "comment": start.get("comment", ""), "sample": start.get("sample"), "time": start.get("time"),
            "stop_time": stop.get("time"), "exit_status": stop.get("exit_status"),
            "num_events": (stop.get("num_events") or {}).get("primary"), "num_points": start.get("num_points"),
            "run_kind": start.get("run_kind"), "setup": start.get("setup"), "elog_logbook": start.get("elog_logbook")}


def table(run, stream: str = "primary", columns: list | None = None):
    """Scalar columns of a stream as a DataFrame (arrays / camera frames are left out), with the
    run's derived values computed per row (inputs from other streams interpolated in time)."""
    from fastq import derived as D

    defs = {d["name"]: d for d in _derived(run)}
    if not defs:
        return _table(run, stream, columns)
    if columns is None:
        wanted = {k for k, v in data_keys(run, stream).items() if "derived" in v}
        own = _raw_keys(run, stream)
        raw = [k for k, v in own.items() if len(v.get("shape") or ()) <= 1] + (["time"] if wanted else [])
    else:
        wanted = set(columns) & set(defs)
        own = _raw_keys(run, stream)
        raw = [c for c in columns if c not in defs]
        raw += [k for n in wanted for k in defs[n]["keys"].values() if k in own and k not in raw]
        if wanted and "time" not in raw:
            raw.append("time")
    df = _table(run, stream, raw)
    if wanted:
        cache: dict = {}

        def other(key):
            if key not in cache:
                cache[key] = None
                for s in run:
                    if s != stream and key in _raw_keys(run, s):
                        t = _table(run, s, ["time", key])
                        cache[key] = (t["time"].to_numpy(dtype=float), t[key].to_numpy(dtype=float))
                        break
                else:
                    conf = _config_values(run)
                    if key in conf:  # a configuration value: constant over the run
                        cache[key] = (np.array([0.0]), np.array([conf[key]]))
            return cache[key]
        D.add_columns(df, list(defs.values()), wanted, other)
    return df


def _table(run, stream: str = "primary", columns: list | None = None):
    import pandas as pd

    node = run[stream]
    dk = (node.metadata or {}).get("data_keys") or {}
    scalar = [k for k, v in dk.items() if len(v.get("shape") or ()) <= 1]
    want = [c for c in (columns or scalar) if c in scalar or c in TIME_COLS or c == "seq_num"]
    try:
        variables = [c for c in want if c != "seq_num"]  # time is a column of the stream table too
        ds = node.read(variables=variables) if variables else node.read()
        df = ds.to_dataframe()
    except Exception:  # a running run: columns can be momentarily unequal -> column by column
        cols = {}
        for key in want:
            try:
                arr = np.asarray(node[key].read())
            except Exception:  # noqa: BLE001
                continue
            if arr.ndim == 1:
                cols[key] = arr
        n = min((len(a) for a in cols.values()), default=0)
        df = pd.DataFrame({k: a[:n] for k, a in cols.items()})
    if "time" not in df.columns and df.index.name == "time":
        df = df.reset_index()
    if "seq_num" not in df.columns:
        df.insert(0, "seq_num", np.arange(1, len(df) + 1))
    return df


# ------------------------------------------------------------------ plots
def _expand(patterns, columns) -> list[str]:
    out = []
    for p in patterns or []:
        hits = [c for c in columns if fnmatch.fnmatchcase(c, p)] if any(ch in p for ch in "*?[") else [p]
        out += [h for h in hits if h in columns and h not in out]
    return out


def _clean(a) -> list:
    return [None if (v is None or (isinstance(v, float) and not math.isfinite(v))) else v
            for v in np.asarray(a, dtype=float).tolist()]


def _avg(df, x, y):
    g = df.groupby(x, sort=True)[y]
    mean, count, std = g.mean(), g.count(), g.std()
    sem = (std / np.sqrt(count.clip(lower=1))).fillna(0.0)
    return mean.index.to_numpy(), mean.to_numpy(), sem.to_numpy()


def plot_data(df, spec: dict) -> dict:
    """Series for one plot spec (see the module docstring)."""
    cols = list(df.columns)
    kind = spec.get("kind", "line")
    x = spec.get("x") or ("time" if "time" in cols else "seq_num")
    if spec.get("last"):
        df = df.tail(int(spec["last"]))
    ys = _expand(spec.get("y"), cols) or [c for c in cols if c not in (x, "seq_num", "time")][:1]
    out = {"kind": kind, "x_label": x, "y_label": ", ".join(ys) if len(ys) < 4 else f"{len(ys)} columns",
           "n": int(len(df)), "series": [], "title": spec.get("title")}
    if x not in cols or not len(df):
        return out
    if kind == "heatmap":
        z = spec.get("z") or (ys[1] if len(ys) > 1 else None)
        yv = ys[0] if ys else None
        if not z or yv not in cols or z not in cols:
            return out
        pv = df.pivot_table(index=yv, columns=x, values=z, aggfunc="mean")
        out.update(x=_clean(pv.columns), y=_clean(pv.index), z=[_clean(r) for r in pv.to_numpy()], z_label=z,
                   y_label=yv)
        return out
    group = spec.get("group") if spec.get("group") in cols else None
    average, combine = bool(spec.get("average")), spec.get("combine")
    for yc in ys:
        if yc not in cols:
            continue
        sub = df[[x, yc] + ([group] if group else [])].dropna(subset=[x])
        if group and combine:
            levels = sorted(sub[group].dropna().unique())
            if len(levels) != 2:
                continue
            lo, hi = (_avg(sub[sub[group] == lv], x, yc) for lv in levels)
            xs = np.intersect1d(lo[0], hi[0])
            a = lo[1][np.searchsorted(lo[0], xs)]
            b = hi[1][np.searchsorted(hi[0], xs)]
            ea, eb = lo[2][np.searchsorted(lo[0], xs)], hi[2][np.searchsorted(hi[0], xs)]
            with np.errstate(divide="ignore", invalid="ignore"):
                if combine == "ratio":
                    val, err = b / a, np.abs(b / a) * np.hypot(ea / a, eb / b)
                elif combine == "rel_diff":
                    val, err = (b - a) / a, np.abs(b / a) * np.hypot(ea / a, eb / b)
                else:
                    val, err = b - a, np.hypot(ea, eb)
            label = {"ratio": "/", "rel_diff": "rel. Δ", "diff": "Δ"}.get(combine, combine)
            out["series"].append({"name": f"{yc} ({label} {group} {levels[1]:g} vs {levels[0]:g})",
                                  "x": _clean(xs), "y": _clean(val), "err": _clean(err)})
            continue
        parts = [(None, sub)] if not group else [(lv, sub[sub[group] == lv]) for lv in sorted(sub[group].dropna().unique())]
        for lv, part in parts:
            name = yc if lv is None else f"{yc} [{group}={lv:g}]" if isinstance(lv, (int, float, np.number)) else f"{yc} [{lv}]"
            if average:
                xs, ys_, err = _avg(part, x, yc)
                out["series"].append({"name": name, "x": _clean(xs), "y": _clean(ys_), "err": _clean(err)})
            else:
                out["series"].append({"name": name, "x": _clean(part[x]), "y": _clean(part[yc])})
    return out


def default_plots(start: dict, streams: dict) -> list[dict]:
    """THE plots of a run (the one place they are made): explicit md["plots"] if a run has them,
    else built from md["plot_hints"] {x, signals, group, average, map_y} that plans write, falling
    back to the bluesky hints (dimensions, motors, detectors). streams: {stream: data_keys} (with the
    derived values, see data_keys). Signal vs x (one curve per group value, averaged over repeats),
    their difference for a two-valued group, a map for 2D scans, I-V curves, one small trend per
    monitored signal (its derived values where they exist)."""
    if start.get("plots"):
        return [dict({"stream": "primary"}, **p) for p in start["plots"]]
    hints = start.get("plot_hints") or {}
    prim = streams.get("primary") or {}
    cols = [k for k, v in prim.items() if len(v.get("shape") or ()) <= 1]
    dims = (start.get("hints") or {}).get("dimensions") or []
    x = hints.get("x") if hints.get("x") in cols + ["time"] else None
    x = x or next((f[0][0] for f in dims if f and f[0] and f[0][0] in cols), None) or         next((m for m in start.get("motors") or [] if m in cols), None) or "time"
    derived = [k for k, v in prim.items() if "derived" in v]
    ys = [y for y in hints.get("signals") or [] if y in cols] or derived
    if not ys:
        detectors = start.get("detectors") or []
        ys = [c for c in cols if any(c.startswith(d + "_") or c == d for d in detectors)]
    ys = [c for c in ys if c != x and not c.endswith("_array_counter")] or         [c for c in cols if c not in (x, "time", "repeat") and not c.endswith("_array_counter")][:1]
    group = hints.get("group") if hints.get("group") in cols else None
    average = hints.get("average", "repeat" in cols and x != "repeat")
    plots = []
    if prim and ys:
        plots.append({"title": "Signal", "stream": "primary", "x": x, "y": ys[:1], "group": group, "average": average})
        if group:
            plots.append({"title": "Difference", "stream": "primary", "x": x, "y": ys[:1], "group": group,
                          "combine": "diff"})
        if hints.get("map_y") in cols:
            plots.append({"title": "Map", "stream": "primary", "kind": "heatmap", "x": x, "y": [hints["map_y"]],
                          "z": ys[0]})
    if "iv" in streams:
        plots.append({"title": "I-V", "stream": "iv", "x": "current_a", "y": ["voltage_v"], "group": "curve",
                      "kind": "scatter"})
    for st, keys in streams.items():
        if st.endswith("_monitor") and st not in ("phase_monitor", "condition_monitor"):
            sig = st[: -len("_monitor")]
            ders = [k for k, v in (keys or {}).items() if "derived" in v]
            if ders or sig in (keys or {}):
                plots.append({"title": ", ".join(ders) or sig, "stream": st, "x": "time", "y": ders or [sig],
                              "small": True})
    return plots[:8]


# ------------------------------------------------------------------ notebook / export
class RunView:
    def __init__(self, uid: str, node) -> None:
        self.uid, self.node = uid, node
        self.md = (node.metadata or {}).get("start") or {}
        self.stop = (node.metadata or {}).get("stop") or {}

    @property
    def streams(self) -> list[str]:
        return list(self.node)

    def table(self, stream: str = "primary", columns: list | None = None):
        return table(self.node, stream, columns)

    def frames(self, key: str, stream: str = "primary"):
        return self.node[stream][key]

    def plot(self, spec: dict) -> dict:
        return plot_data(self.table(spec.get("stream", "primary")), spec)

    def __repr__(self) -> str:
        return (f"<Run {self.md.get('scan_id')} {self.md.get('plan_name')} {self.uid[:8]} "
                f"{self.stop.get('exit_status', 'running')} {self.md.get('comment', '')!r}>")


def load_run(ref=-1, client=None, lab: dict | None = None) -> RunView:
    client = client or tiled_client(lab)
    uid, node = find_run(client, ref)
    return RunView(uid, node)


def export_hdf5(run: RunView, path: str | Path, frames: bool = False) -> Path:
    """One HDF5 file per run: root attributes start / stop (JSON), /<stream>/<column> datasets (derived
    values included) and,
    with frames=True, the camera frames too (else: their file and dataset as attributes)."""
    import json

    import h5py

    path = Path(path)
    with h5py.File(path, "w") as f:
        f.attrs["start"] = json.dumps(run.md, default=str)
        f.attrs["stop"] = json.dumps(run.stop, default=str)
        for s in run.streams:
            g = f.create_group(s)
            df = run.table(s)
            for c in df.columns:
                try:
                    g.create_dataset(c, data=df[c].to_numpy())
                except TypeError:
                    g.create_dataset(c, data=df[c].astype(str).to_numpy().astype("S"))
            for key, dk in data_keys(run.node, s).items():
                if len(dk.get("shape") or ()) > 1:
                    if frames:
                        g.create_dataset(key, data=run.frames(key, s).read(), compression="gzip")
                    else:
                        g.attrs[f"{key}.source"] = str(dk.get("source"))
    return path
