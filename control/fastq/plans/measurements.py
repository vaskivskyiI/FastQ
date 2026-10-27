"""Measurement plans - one Bluesky run each (start/stop documents, metadata,
streams). These are what gets queued on the queue server.

Data layout of a run:
  primary               discrete readings (trigger_and_read)
  <signal>_monitor      continuous monitor streams: every IOC update of the
                        monitored PVs with its EPICS timestamp (lock-in X/Y,
                        temperature, laser readbacks, ...), and the bookkeeping
                        signals `phase`, `condition` and each scan dimension
Plans that emit light or current always end in a safe state (finalize).
Quantities such as a sample resistance are not measured by an instrument: they are
derived values of the experiment (fastq.derived), computed from these raw streams.

Conventions (every plan, see control/experiments/README.md): devices are parameters named
after their lab role (temperature=, lockin=, laser=, keithley=), None = the role's device;
quantities carry their unit (_s, _k, _a, _v, _fs, _percent, _k_per_min ...); `settle_s` /
`tolerance_k` are the temperature wait. Run metadata: detectors / motors (bluesky), run_kind,
plot_hints {x, signals, group, average, map_y} - the plots come from fastq.analysis.default_plots.
"""
from __future__ import annotations

from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp
from ophyd import Signal

from fastq.conditions import build_conditions, substitute
from fastq.plans import keithley as K
from fastq.plans import laser as L
from fastq.plans import lockin as LI
from fastq.plans import temperature as T
from fastq.session import resolve


# ---------------------------------------------------------------- helpers
def _names(*devices) -> list[str]:
    return [d.name for d in devices if d is not None]


def _lockin_monitors(lockin) -> list:
    """What a derived resistance needs: X, Y and the excitation amplitude (it may change in the run)."""
    return [lockin.x, lockin.y, lockin.amplitude]


def _derived_of(dev) -> list[str]:
    """Names of the experiment's derived values that use this device."""
    from fastq import derived as D
    from fastq.session import load_lab

    try:
        return [d["name"] for d in D.load(load_lab()) if any(r.split(".")[0] == dev.name for r in d["inputs"].values())]
    except Exception:  # noqa: BLE001
        return []


def _segments(segments, start_k, final_k, rate_k_per_min, time_min):
    if segments:
        return segments
    if rate_k_per_min is None:
        rate_k_per_min = abs(final_k - start_k) / time_min
    return [[start_k, final_k, rate_k_per_min]]


# ---------------------------------------------------------------- plans
def set_temperature(setpoint_k: float, tolerance_k: float = 0.2, settle_s: float = 20.0, temperature=None):
    """Go to setpoint and wait until stable (no run / no data)."""
    yield from T.stabilize(resolve(temperature, "temperature"), setpoint_k, tolerance_k, settle_s)


def rt_sweep(temperature=None, detectors: list = (), segments: list | None = None, start_k: float | None = None,
             final_k: float | None = None, rate_k_per_min: float | None = None, time_min: float | None = None,
             period_s: float = 0.5, go_to_start: bool = True, final_tolerance_k: float = 0.05,
             lockin=None, current_a: float | None = None, series_ohm: float | None = None,
             lockin_hz: float | None = 10.0, md: dict | None = None):
    """R(T): (stabilize at the start and) sweep through segments
    [[from_K, to_K, K/min], ...] (or start_k/final_k + rate or time),
    reading `detectors` + the controller every period_s. A lock-in among the
    detectors is also monitored continuously (X / Y / amplitude at lockin_hz);
    current_a sets its excitation through the series resistor."""
    temperature = resolve(temperature, "temperature")
    detectors = [resolve(d) for d in detectors]
    segs = _segments(segments, start_k, final_k, rate_k_per_min, time_min)
    lockin = resolve(lockin) or next((d for d in detectors if hasattr(d, "amplitude") and hasattr(d, "x")), None)
    monitors = [temperature.temp] + (_lockin_monitors(lockin) if lockin else [])
    signals = (_derived_of(lockin) if lockin else []) + \
        [f for d in detectors for f in (getattr(d, "hints", None) or {}).get("fields", [])]
    md = {"run_kind": "R(T)", "segments": segs, "detectors": _names(*detectors), "motors": _names(temperature),
          "plot_hints": {"x": temperature.temp.name, "signals": signals}, **(md or {})}

    def read():
        yield from bps.trigger_and_read(list(detectors) + [temperature])
        yield from bps.sleep(period_s)

    @bpp.monitor_during_decorator(monitors)
    @bpp.run_decorator(md=md)
    def inner():
        if lockin is not None and current_a:
            yield from LI.set_excitation(lockin, current_a, series_ohm)
        if lockin is not None and lockin_hz:
            yield from LI.sample_rate(lockin, lockin_hz)
        yield from T.sweep(temperature, segs, final_tolerance_k=final_tolerance_k, go_to_start=go_to_start,
                           during=read)

    return (yield from bpp.finalize_wrapper(inner(), T.stop_sweep(temperature)))


def iv_curves(currents_a: list | None = None, max_current_a: float = 1e-3, points_per_quadrant: int = 500,
              spacing_exponent: float = 2.0, temperatures_k: list | None = None, compliance_v: float = 5.0,
              nplc: float = 1.0, first_settle_s: float = 1.0, point_settle_s: float = 0.01,
              tolerance_k: float = 0.05, settle_s: float = 20.0, keithley=None, temperature=None,
              md: dict | None = None):
    """DC I-V curve(s) with the Keithley, at the current temperature or at
    each of `temperatures_k`. Stream "iv": one event per point (curve,
    current_a, voltage_v, t_s, temperature_k); "primary": one summary event
    per curve. Default current list: 0 -> +max -> 0 -> -max -> 0 with
    (i/(n-1))^spacing per quadrant."""
    keithley = resolve(keithley, "keithley")
    # a temperature controller is needed only to loop over temperatures (otherwise just logged, if present)
    temperature = resolve(temperature, "temperature", required=bool(temperatures_k))
    currents = currents_a or iv_current_list(max_current_a, points_per_quadrant, spacing_exponent)
    if max(abs(c) for c in currents) > 0.105:
        raise ValueError("current above the 6221 limit")
    md = {"run_kind": "I-V", "n_points": len(currents), "temperatures_k": temperatures_k,
          "detectors": _names(keithley), "motors": _names(temperature) if temperatures_k else [], **(md or {})}

    @bpp.run_decorator(md=md)
    def inner():
        yield from bps.mv(keithley.mode, "DCSweep", keithley.compliance, compliance_v, keithley.nplc, nplc,
                          keithley.dc_settle_s, first_settle_s, keithley.dc_point_settle_s, point_settle_s)
        yield from bps.mv(keithley.current_list, list(currents))
        for n, t in enumerate(temperatures_k or [None], start=1):
            if t is not None:
                yield from T.stabilize(temperature, t, tolerance_k, settle_s)
            yield from K.record_sweep(keithley, temperature, n)  # points live in "iv", then the summary

    return (yield from bpp.finalize_wrapper(inner(), bps.mv(keithley.abort_cmd, 1)))


def iv_current_list(max_abs_a: float, points_per_quadrant: int, exponent: float = 1.0) -> list[float]:
    n = int(points_per_quadrant)
    pos = [max_abs_a * (i / (n - 1)) ** exponent for i in range(n)]
    return pos + pos[-2::-1] + [-v for v in pos[1:]] + [-v for v in pos[-2:0:-1]] + [0.0]


def aux_waveform(aux_index: int = 1, points: list | None = None, start_v: float | None = None,
                 stop_v: float | None = None, rate_v_per_s: float | None = None, update_s: float = 0.05,
                 lockin=None, md: dict | None = None):
    """Drive a lock-in AuxOut (front-panel number; linear ramp start_v -> stop_v at rate_v_per_s,
    or piecewise-linear through points [[t_s, V], ...]) while monitoring the lock-in."""
    lockin = resolve(lockin, "lockin")
    aux = getattr(lockin, f"aux_out{aux_index}")
    md = {"run_kind": "Aux waveform", "detectors": _names(lockin), "plot_hints": {"x": "time", "signals": [aux.name]},
          **(md or {})}

    @bpp.monitor_during_decorator([aux, *_lockin_monitors(lockin)])
    @bpp.run_decorator(md=md)
    def inner():
        if points:
            yield from LI.aux_waveform(lockin, aux_index, points, update_s)
        else:
            yield from LI.aux_ramp(lockin, aux_index, start_v, stop_v, rate_v_per_s, update_s)

    return (yield from inner())


# ---------------------------------------------------------------- laser sequences
def laser_sequence_scan(sequence: list, dimensions: dict | None = None, randomized: list | None = None,
                        nesting: list | None = None, temperatures_k: list | None = None,
                        current_a: float | None = 1e-6, series_ohm: float | None = None,
                        lockin_hz: float = 10.0, probe=None, probe_settings: dict | None = None,
                        tolerance_k: float = 0.2, settle_s: float = 20.0, seed: int | None = None,
                        laser_limits: dict | None = None, laser=None, lockin=None, temperature=None,
                        md: dict | None = None):
    """Generic laser experiment. For each temperature (outermost, optional)
    and each condition of `dimensions` (ordered by nesting/randomized), run
    `sequence`: a list of steps
        {"kind": "reference", "duration_s": 10, "phase": "before"}
        {"kind": "pulses", "duration_fs": 221, "attenuator_percent": 12, "pulse_count": 10, ...}
        {"kind": "ramp", "duration_fs": 221, "start_percent": 16, "stop_percent": 0, "divider": 500,
         "rate_v_per_s": 0.05, "phase": "ramp"}
        {"kind": "wait", "duration_s": 10, "phase": "after"}
    where any value may be "$<dimension>". Resistance is monitored
    continuously (lock-in, or a Keithley in continuous delta: probe=keithley).
    laser_limits: temporary interlocks for THIS run only, e.g.
    {"max_attenuator_percent": 5, "min_pp_divider": 10000}. A temperature controller
    is needed only to loop over temperatures_k (otherwise just logged, if present)."""
    laser, lockin = resolve(laser, "laser"), resolve(lockin, "lockin")
    temperature = resolve(temperature, "temperature", required=bool(temperatures_k))
    probe = resolve(probe)
    dimensions = dimensions or {}
    conditions = build_conditions(dimensions, randomized, nesting, seed)
    L.set_limits(laser_limits)
    try:
        for cond in conditions:  # validate everything BEFORE the run: $names and laser limits
            for step in substitute(sequence, cond):
                if step["kind"] == "pulses":
                    L.check_limits(step["attenuator_percent"],
                                   step.get("burst_divider", 1000) if step.get("mode") == "burst" else None)
                elif step["kind"] == "ramp":
                    L.check_limits(max(step["start_percent"], step["stop_percent"]), step["divider"])
    except Exception:
        L.set_limits(None)
        raise
    phase = Signal(name="phase", value="")
    condition = Signal(name="condition", value=0)
    dim_sigs = {name: Signal(name=name, value=values[0]) for name, values in dimensions.items()}
    monitors = [phase, condition, *dim_sigs.values(), laser.shutter_open, laser.pp_enable, laser.attenuator,
                laser.pulse_duration, lockin.aux_out1]
    if probe is not None:
        monitors.append(probe.last_reading)
    else:
        monitors += _lockin_monitors(lockin)
    if temperature is not None:
        monitors.append(temperature.temp)
    signals = [probe.last_reading.name] if probe is not None else _derived_of(lockin) or [lockin.x.name]
    md = {"run_kind": "Laser sequence", "sequence": sequence, "dimensions": dimensions,
          "randomized": randomized or [], "nesting": nesting or [], "temperatures_k": temperatures_k,
          "conditions_per_temperature": len(conditions), "laser_limits": dict(L.LIMITS),
          "detectors": _names(lockin, probe), "motors": _names(laser, temperature),
          "plot_hints": {"x": "time", "signals": signals}, **(md or {})}

    def mark(value):
        yield from bps.mv(phase, value)

    def run_step(step):
        kind = step["kind"]
        args = {k: v for k, v in step.items() if k != "kind"}
        if kind == "reference":
            yield from L.reference(laser, mark=mark, **args)
        elif kind == "wait":
            yield from mark(args.get("phase", "wait"))
            yield from bps.sleep(args["duration_s"])
        elif kind == "pulses":
            yield from L.pulses(laser, mark=mark, **args)
        elif kind == "ramp":
            yield from L.ramp(laser, lockin, mark=mark, **args)
        else:
            raise ValueError(f"unknown sequence step kind {kind!r}")

    @bpp.monitor_during_decorator(monitors)
    @bpp.run_decorator(md=md)
    def inner():
        yield from bps.mv(laser.scan, ".2 second")  # faster shutter/PP confirmation
        yield from L.safe_state(laser, lockin)
        if probe is None:
            if current_a:
                yield from LI.set_excitation(lockin, current_a, series_ohm)
            yield from LI.sample_rate(lockin, lockin_hz)
        else:
            yield from start_continuous_delta(probe, **(probe_settings or {}))
        index = 0
        for t in (temperatures_k or [None]):
            if t is not None:
                yield from T.stabilize(temperature, t, tolerance_k, settle_s)
            for cond in build_conditions(dimensions, randomized, nesting, seed):
                index += 1
                args = [condition, index]
                for name, value in cond.items():
                    args += [dim_sigs[name], value]
                yield from bps.mv(*args)
                for step in substitute(sequence, cond):
                    yield from bps.checkpoint()
                    yield from run_step(step)

    def finish():
        try:
            yield from L.safe_state(laser, lockin)
            if probe is not None:
                yield from bps.mv(probe.stop_cmd, 1)
        finally:
            L.set_limits(None)  # limits apply to this run only

    return (yield from bpp.finalize_wrapper(inner(), finish()))


def start_continuous_delta(keithley, current_a: float = 1e-6, delay_s: float = 0.02, compliance_v: float = 10.0):
    """Keithley continuous delta (DeltaCount 0) as a live resistance probe
    (LastReading); ended by keithley.stop_cmd."""
    yield from K.configure_delta(keithley, current_a, 0, delay_s, compliance_v)
    status = yield from bps.abs_set(keithley.acquire_cmd, 1)  # runs until Stop; completion not awaited
    for _ in range(300):
        if (yield from bps.rd(keithley.state)) == "Running":
            return
        if status.done and not status.success:  # refused: another run is still going
            status.wait(0)
        yield from bps.sleep(0.1)
    raise TimeoutError("Keithley continuous delta did not start")
