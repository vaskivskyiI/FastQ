"""Electrical transport plans - the measurements of the TransportMeasure GUI
(Cooling mode, Delta, Pulse Delta, Sweep Pulse, single pulse, continuous R during a ramp) as
Bluesky plans; the DC sweep is general.iv_curves. Devices: keithley= (6221 + 2182A),
temperature= (any controller for the steps; the ramp interface of ITC503 / Mercury iTC /
Lake Shore 336 for continuous R(T)), lockin=; None = the lab role's device.

Resistance R = mean voltage / source current is computed at every point and
stored next to the raw Keithley results (mean, std, N).
"""
from __future__ import annotations

from decimal import Decimal

from bluesky import plan_stubs as bps
from ophyd import Signal

from fastq.plans import keithley as K
from fastq.plans import measurements as M
from fastq.plans.scan import nd_scan
from fastq.session import resolve


def temperature_list(table: list) -> list[float]:
    """TransportMeasure's temperature table: [[start, stop, step], ...] -> points with
    the stop value of every range included and duplicates removed (gen_meas_list)."""
    out: list[float] = []
    for start, stop, step in table:
        a, b, d = Decimal(str(start)), Decimal(str(stop)), abs(Decimal(str(step)))
        d = d if b >= a else -d
        x = a
        while (x <= b) if d > 0 else (x >= b):
            out.append(float(x))
            x += d
        out.append(float(b))
    seen, result = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _resistance(k, current_a):
    sig = Signal(name="resistance", value=float("nan"))
    return [(sig, lambda: float(k.mean.get()) / current_a if current_a else float("nan"))]


def _context(keithley, temperature, required_t: bool = False):
    return resolve(keithley, "keithley"), resolve(temperature, "temperature", required=required_t)


def tm_rt_steps(temperatures_k: list | None = None, table_k: list | None = None, tolerance_k: float = 0.1,
                settle_s: float = 60.0, current_a: float = 1e-6, count: int = 10, delay_s: float = 0.02,
                compliance_v: float = 10.0, pulsed: bool = False, width_s: float = 1e-3, repeats: int = 1,
                with_lockin: bool = False, keithley=None, temperature=None, lockin=None):
    """Cooling mode: go to each temperature, wait until |T - setpoint| <= tolerance_k has held
    for settle_s (leaving the window restarts the wait), then measure R with the Keithley in
    Delta (or Pulse Delta) mode. temperatures_k: list, or table_k [[start, stop, step], ...]."""
    k, temp = _context(keithley, temperature, required_t=True)
    temps = list(temperatures_k) if temperatures_k else temperature_list(table_k or [[300, 10, 10]])
    if pulsed:
        yield from K.configure_pulse_delta(k, current_a, 0.0, width_s, 16e-6, count, 5, compliance_v)
    else:
        yield from K.configure_delta(k, current_a, count, delay_s, compliance_v)
    axes = [{"device": temp, "values": temps, "tolerance": tolerance_k, "settle_s": settle_s, "label": "temperature"}]
    if repeats != 1:
        axes.append({"repeat": repeats})
    dets = [k] + ([resolve(lockin, "lockin")] if with_lockin else [])
    return (yield from nd_scan(axes, detectors=dets, readables=[temp], computed=_resistance(k, current_a),
                               md={"run_kind": "R(T) steps", "current_a": current_a},
                               safe_state={k.abort_cmd: 1}))


def tm_delta(points: int = 20, current_a: float = 1e-6, count: int = 10, delay_s: float = 0.02,
             compliance_v: float = 10.0, keithley=None, temperature=None):
    """Delta mode at fixed conditions: `points` readings of mean/std over `count` deltas each."""
    k, temp = _context(keithley, temperature)
    yield from K.configure_delta(k, current_a, count, delay_s, compliance_v)
    return (yield from nd_scan([{"repeat": points}], detectors=[k], readables=[temp] if temp else [],
                               computed=_resistance(k, current_a), md={"run_kind": "Delta", "current_a": current_a},
                               safe_state={k.abort_cmd: 1}))


def tm_pulse_delta(points: int = 20, current_a: float = 1e-3, low_a: float = 0.0, width_s: float = 1e-3,
                   source_delay_s: float = 16e-6, count: int = 10, interval_plc: int = 5, compliance_v: float = 10.0,
                   keithley=None, temperature=None):
    """Pulse Delta at fixed conditions (6221 pulse + 2182A), R = V / I_high."""
    k, temp = _context(keithley, temperature)
    yield from K.configure_pulse_delta(k, current_a, low_a, width_s, source_delay_s, count, interval_plc,
                                      compliance_v)
    return (yield from nd_scan([{"repeat": points}], detectors=[k], readables=[temp] if temp else [],
                               computed=_resistance(k, current_a),
                               md={"run_kind": "PulseDelta", "current_a": current_a},
                               safe_state={k.abort_cmd: 1}))


def tm_rt_continuous(start_k: float = 300.0, final_k: float = 10.0, rate_k_per_min: float | None = 1.0,
                     time_min: float | None = None, current_a: float = 1e-6, count: int = 10, delay_s: float = 0.02,
                     compliance_v: float = 10.0, nplc: float | None = None, keithley=None, temperature=None):
    """Continuous R(T): stabilise at start_k, ramp to final_k (at rate_k_per_min, or in time_min)
    and measure Delta (mean of `count`) back to back during the ramp, each with the temperature
    (TransportMeasure's sweep-table scripts). Needs the controller's ramp interface (fastq/roles.py)."""
    k, temp = _context(keithley, temperature, required_t=True)
    yield from K.configure_delta(k, current_a, count, delay_s, compliance_v, nplc)
    return (yield from M.rt_sweep(temp, [k], start_k=start_k, final_k=final_k,
                                  rate_k_per_min=None if time_min else rate_k_per_min, time_min=time_min,
                                  period_s=0.0, md={"run_kind": "R(T) continuous", "current_a": current_a}))


def tm_pulse_sweep(spacing: str = "Linear", start_a: float = 0.0, stop_a: float = 1e-3, step_a: float = 1e-6,
                   points: int = 10, currents_a: list | None = None, reverse: bool = False, width_s: float = 500e-6,
                   source_delay_s: float = 100e-6, interval_plc: int = 5, low_meas: int = 2,
                   compliance_v: float = 1.0, volt_range: float = 0.0, temperatures_k: list | None = None,
                   tolerance_k: float = 0.2, settle_s: float = 10.0, keithley=None, temperature=None):
    """Pulsed I-V (TransportMeasure "Sweep Pulse"): 6221 pulse-delta sweep, Linear (start/stop/step),
    Log (start/stop/points) or List (`currents_a`), optionally reversed; one point per pulse in the
    "iv" stream (current_a, voltage_v, t_s, temperature_k), at each of `temperatures_k` if given."""
    from bluesky import preprocessors as bpp

    k, temp = _context(keithley, temperature, required_t=bool(temperatures_k))
    if currents_a:
        spacing = "List"
    md = {"run_kind": "PulseSweep", "spacing": spacing, "temperatures_k": temperatures_k,
          "detectors": [k.name], "motors": [temp.name] if temperatures_k else []}

    @bpp.run_decorator(md=md)
    def inner():
        yield from bps.mv(k.mode, "PulseSweep", k.compliance, compliance_v, k.ps_spacing, spacing,
                          k.ps_start, start_a, k.ps_stop, stop_a, k.ps_step, step_a, k.ps_points, points,
                          k.ps_reverse, int(reverse), k.pd_width, width_s, k.pd_source_delay, source_delay_s,
                          k.pd_interval_plc, interval_plc, k.pd_low_meas, low_meas, k.volt_range, volt_range)
        if currents_a:
            yield from bps.mv(k.current_list, list(currents_a))
        for n, t in enumerate(temperatures_k or [None], start=1):
            if t is not None:
                yield from bps.abs_set(temp, t, tolerance=tolerance_k, settle_s=settle_s, wait=True)
            yield from K.record_sweep(k, temp, n)

    return (yield from bpp.finalize_wrapper(inner(), bps.mv(k.abort_cmd, 1)))


def tm_single_pulse(amplitude_a: float = 500e-6, width_s: float = 100e-6, compliance_v: float = 1.0,
                    volt_range: float = 10.0, keithley=None, temperature=None):
    """One current pulse (TransportMeasure Settings tab: write/switching pulse); the reading is kept."""
    k, temp = _context(keithley, temperature)
    yield from bps.mv(k.mode, "SinglePulse", k.compliance, compliance_v, k.sp_amplitude, amplitude_a,
                      k.sp_width, width_s, k.volt_range, volt_range)
    return (yield from nd_scan([{"repeat": 1}], detectors=[k], readables=[temp] if temp else [],
                               md={"run_kind": "SinglePulse", "amplitude_a": amplitude_a, "width_s": width_s},
                               safe_state={k.abort_cmd: 1}))
