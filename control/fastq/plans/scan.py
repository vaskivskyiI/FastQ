"""One general N-dimensional scan: nested axes (outermost first), each point =
move what changed (set-and-confirm), then acquire. Covers the 4dmag sequences
(repeat x rotator x delay x waveplate x pump shutter) and the transport
temperature / field / current loops.

    nd_scan(axes=[{"repeat": 100},
                  {"device": "rotator1", "values": [0, 90]},
                  {"device": "delay1", "start": -10, "stop": 150, "num": 81},
                  {"device": "pump", "signal": "shutter", "values": [0, 1]}],
            detectors=["cam_main", "cam_ref"], pulser="pump", readables=["flow1"])

Axis keys
  device      device (or its name); "signal": attribute to set instead of the device
              itself (e.g. "shutter" of the beam shutter)
  values | start+stop+num | start+stop+step (inclusive) | "list" alias of values
  relative    values are offsets from the position at scan start
  tolerance, settle_s, timeout_s   set-and-confirm overrides (devices that wait for
              their readback: motors, temperature, field, flow)
  snake       reverse this axis on every other pass of the axis outside it
  zip         [{device, values}, ...] instead of device: move several together
  repeat      N: a repeat counter axis (N = 0: forever, until the run is stopped -
              "Stop" ends it cleanly with exit status success; outermost only)
Acquisition at every point: trigger `detectors` (together), read them,
`readables` and every axis device into one event. With `pulser` (a device with a Trigger action) the
detectors are armed together and the pulser fires until all have their frame
(fastq.detectors.synced_trigger_and_read). `monitors` are recorded
continuously (every update, own streams).
"""
from __future__ import annotations

import itertools
import math
import time
import uuid

import numpy as np
from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp
from bluesky.utils import Msg
from ophyd import Signal

from fastq.detectors import synced_trigger_and_read
from fastq.session import resolve

SET_KEYS = ("tolerance", "settle_s", "timeout_s")


def axis_values(ax: dict) -> list:
    if "values" in ax or "list" in ax:
        return list(ax.get("values", ax.get("list")))
    if {"start", "stop", "num"} <= ax.keys():
        return np.linspace(ax["start"], ax["stop"], int(ax["num"])).tolist()
    if {"start", "stop", "step"} <= ax.keys():
        n = int(math.floor(abs((ax["stop"] - ax["start"]) / ax["step"]) + 1e-9)) + 1
        step = math.copysign(abs(ax["step"]), ax["stop"] - ax["start"])
        return [ax["start"] + i * step for i in range(n)]
    raise ValueError(f"axis {ax}: give values, start/stop/num or start/stop/step")


class Axis:
    def __init__(self, spec: dict, index: int) -> None:
        self.spec = dict(spec)
        self.repeat = "repeat" in spec
        self.snake = bool(spec.get("snake"))
        if self.repeat:
            self.count = int(spec["repeat"])
            self.label = spec.get("label", "repeat")
            self.targets = []
            self.counter = Signal(name=self.label, value=0)
            return
        members = spec["zip"] if "zip" in spec else [spec]
        self.targets = []
        for m in members:
            dev = resolve(m["device"])
            if dev is None or isinstance(dev, str):
                raise LookupError(f"axis {index}: unknown device {m['device']!r}")
            obj = getattr(dev, m["signal"]) if m.get("signal") else dev
            self.targets.append((dev, obj, axis_values(m), {k: m[k] for k in SET_KEYS if k in m}))
        lengths = {len(v) for _, _, v, _ in self.targets}
        if len(lengths) != 1:
            raise ValueError(f"axis {index}: zipped value lists differ in length {sorted(lengths)}")
        self.count = lengths.pop()
        self.label = spec.get("label") or "+".join(o.name for _, o, _, _ in self.targets)
        if spec.get("relative"):
            self.targets = [(d, o, [v + _position(o) for v in vals], kw) for d, o, vals, kw in self.targets]

    def positions(self, i: int) -> list:
        return [(obj, vals[i], kw) for _, obj, vals, kw in self.targets]

    @property
    def devices(self) -> list:
        return [d for d, *_ in self.targets]


def _jsonable(value):
    """Axis specs for the start document: devices by name (md must be deep-copyable)."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return getattr(value, "name", repr(value))


def _fields(obj) -> list[str]:
    return list((getattr(obj, "hints", None) or {}).get("fields") or [obj.name])


def _position(obj) -> float:
    pos = getattr(obj, "position", None)
    return float(pos if pos is not None else obj.get())


def _index_iter(axes: list[Axis]):
    """Yield index tuples, outermost first; snake axes reverse on alternate outer passes."""
    def rec(level: int, prefix: tuple, flip: bool):
        if level == len(axes):
            yield prefix
            return
        ax = axes[level]
        rng = range(ax.count) if not (ax.snake and flip) else range(ax.count - 1, -1, -1)
        for k, i in enumerate(rng):
            yield from rec(level + 1, prefix + (i,), k % 2 == 1)

    if axes and axes[0].repeat and axes[0].count == 0:  # forever
        for r in itertools.count():
            for rest in rec(1, (), False):
                yield (r,) + rest
    else:
        yield from rec(0, (), False)


def plot_hints(built: list, dets: list, derived: list, computed: list) -> dict:
    """What the scan is about, for fastq.analysis.default_plots: the signal(s), the innermost real
    axis as x, a two-valued axis (pump on/off) as group, averaging over repeats / other axes, and
    the outer axis of a 2D scan as map_y."""
    axes = [a for a in built if a.targets and a.count > 1]
    signals = [s.name for s, *_ in derived] + [s.name for s, _ in computed]
    for d in dets:
        signals += [f for f in (getattr(d, "hints", None) or {}).get("fields", []) if f not in signals]
        if hasattr(d, "roi_list"):  # cameras: their ROI columns (<cam>_<roi>)
            signals += [f"{d.name}_{r}" for r in d.roi_list()] or [f"{d.name}_array_counter"]
    swept = [a for a in axes if a.count > 2]
    pair = next((a for a in reversed(axes) if a.count == 2), None)
    # a two-valued axis (pump on/off) is the group; x is the innermost swept axis, else the repeat
    # counter (static on/off runs: one curve per pump state over the repeats), else time
    x_ax = swept[-1] if swept else (None if pair else (axes[-1] if axes else None))
    counter = next((a.counter.name for a in reversed(built) if a.repeat), None)
    maps = [a for a in axes if a.count > 2]
    return {"x": _fields(x_ax.targets[0][1])[0] if x_ax else (counter or "time"), "signals": signals,
            "group": _fields(pair.targets[0][1])[0] if pair else None,
            "average": any(a.repeat for a in built) or len(axes) > 1,
            "map_y": _fields(maps[-2].targets[0][1])[0] if len(maps) >= 2 else None}


def nd_scan(axes: list, detectors: list = (), readables: list = (), pulser=None, monitors: list = (),
            ratios: list = (), pulse_period_s: float = 0.05, acquire_timeout_s: float = 120.0,
            md: dict | None = None, computed: list = (), safe_state: dict | list | None = None,
            period_s: float = 0.0):
    """General nested scan with set-and-confirm moves. See the module docstring.
    ratios: [[numerator, denominator], ...] cameras whose ROI values are divided
    point by point (4dmag main / reference spectrometer) -> "<num>_over_<den>_<roi>".
    computed: [(Signal, function()), ...] (recipes, not the queue): each signal is set from its
    function after the detectors and read with them, e.g. R = V / I.
    safe_state: [[device, signal or "name[.attr]", value], ...] set when the run ends - also after a
    failure or abort (each one attempted), e.g. [["pump.shutter", 0]] from the queue (run metadata keys
    cannot hold dots) or {keithley.abort_cmd: 1} in a recipe.
    period_s: at least this long from one point to the next (a log: [{"repeat": 0}] + period_s)."""
    built = [Axis(a, i) for i, a in enumerate(axes)]
    if any(a.repeat and a.count == 0 for a in built[1:]):
        raise ValueError("repeat: 0 (forever) is only allowed as the outermost axis")
    dets = [resolve(d) for d in detectors]
    reads = [resolve(r) for r in readables]
    mons = [resolve(m) for m in monitors]
    pulser = resolve(pulser)
    axis_devices = list(dict.fromkeys(d for a in built for d in a.devices))
    counters = [a.counter for a in built if a.repeat]
    derived = []  # one scalar per ROI: <num>_over_<den>_<roi>
    for num, den in ratios:
        num, den = resolve(num), resolve(den)
        for i, roi in enumerate(num.roi_list()):
            derived.append((Signal(name=f"{num.name}_over_{den.name}_{roi}", value=float("nan")), num, den, i))

    def compute():
        for sig, func in computed:
            sig.put(func())
        cache = {}
        for sig, num, den, i in derived:
            for cam in (num, den):
                if cam.name not in cache:
                    cache[cam.name] = np.atleast_1d(np.asarray(cam.roi_sums.get(), float))
            a, b = cache[num.name], cache[den.name]
            sig.put(float(a[i] / b[i]) if i < min(a.size, b.size) and b[i] != 0 else float("nan"))

    others = [d for d in dict.fromkeys([*reads, *axis_devices]) if d not in dets]  # each device read once
    others += [s for s, *_ in derived] + [s for s, _ in computed]
    finite = all(a.count > 0 for a in built)
    shape = [a.count for a in built]
    _md = {"run_kind": "Scan", "plan_args": {"axes": _jsonable(axes)},
           "detectors": [d.name for d in dets], "num_points": int(np.prod(shape)) if finite else None,
           "shape": shape if finite else None,
           "motors": [a.label for a in built if not a.repeat],
           "hints": {"dimensions": [(_fields(a.targets[0][1]), "primary") for a in reversed(built) if a.targets][:1]},
           **(md or {})}
    if not _md["hints"]["dimensions"]:
        _md["hints"].pop("dimensions")
    _md.setdefault("plot_hints", plot_hints(built, dets, derived, computed))

    @bpp.stage_decorator(dets)
    @bpp.run_decorator(md=_md)
    def inner():
        last: tuple | None = None
        t_next = 0.0
        for idx in _index_iter(built):
            if period_s > 0:  # a log: wait for the next period (checkpoint first: pausable while waiting)
                wait = t_next - time.monotonic()
                if wait > 0:
                    yield from bps.sleep(wait)
                t_next = time.monotonic() + period_s
            yield Msg("checkpoint")  # pause/resume replays from here: the moves are redone
            group = f"move-{uuid.uuid4().hex[:6]}"
            moved = changed = False
            for level, (ax, i) in enumerate(zip(built, idx)):
                # set an axis when its index changed or any outer one did (nested loops, as the
                # old sequencer: an inner shutter/filter is re-applied after an outer move)
                changed = changed or last is None or last[level] != i
                if not changed:
                    continue
                if ax.repeat:
                    yield from bps.mv(ax.counter, i)
                    continue
                for obj, value, kw in ax.positions(i):
                    yield Msg("set", obj, value, group=group, **kw)
                    moved = True
            if moved:
                yield Msg("wait", None, group=group)
            last = idx
            yield from synced_trigger_and_read(dets, pulser, [*others, *counters], pulse_period_s=pulse_period_s,
                                               timeout_s=acquire_timeout_s,
                                               before_read=compute if derived or computed else None)

    plan = inner()
    for m in mons:
        plan = bpp.monitor_during_wrapper(plan, [m])
    if safe_state:
        plan = bpp.finalize_wrapper(plan, _safe_state(safe_state))
    return (yield from plan)


def _safe_state(targets):
    errors = []
    for obj, value in (targets.items() if isinstance(targets, dict) else targets):
        try:
            yield from bps.mv(resolve(obj), value)
        except Exception as exc:  # noqa: BLE001 - every target is attempted
            errors.append(f"{getattr(obj, 'name', obj)}: {exc}")
    if errors:
        raise RuntimeError("safe state incomplete: " + "; ".join(errors))
