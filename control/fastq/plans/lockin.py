"""Lock-in plan stubs (MFLI device, ioc_host/iocs/mfli_ioc.py)."""
from __future__ import annotations

import time

from bluesky import plan_stubs as bps

SCAN_MENU = {10.0: "10 second", 5.0: "5 second", 2.0: "2 second", 1.0: "1 second",
             0.5: ".5 second", 0.2: ".2 second", 0.1: ".1 second"}


def series_ohm(value: float | None = None) -> float:
    """The series resistor of the two-terminal setup: the constant series_r of the experiment's
    derived values (fastq.derived) - one source of truth for the excitation and the resistance."""
    from fastq import derived as D
    from fastq.session import load_lab

    defined = D.constant(load_lab(), "series_r")
    if value is None:
        if defined is None:
            raise ValueError("no series resistor: define a derived value with constant series_r "
                             "(dashboard: Devices > derived) or pass series_ohm")
        return defined
    if defined is not None and abs(defined - value) > 1e-6 * abs(defined):
        raise ValueError(f"series_ohm {value:g} differs from the derived values' series_r {defined:g} - "
                         "the recorded resistance would be wrong; change one of them")
    return float(value)


def set_excitation(lockin, current_a: float, series_ohm_: float | None = None):
    """Two-terminal excitation: amplitude (peak) = current * series resistor."""
    r = series_ohm(series_ohm_)
    if current_a <= 0 or r <= 0:
        raise ValueError("current_a and the series resistor must be > 0")
    yield from bps.mv(lockin.amplitude, current_a * r)


def sample_rate(lockin, hz: float):
    """How often the IOC samples X/Y/Resistance (IOC-side only, no instrument write)."""
    period = round(1.0 / hz, 6)
    if period in SCAN_MENU:
        yield from bps.mv(lockin.fast_active, 0)
        yield from bps.mv(lockin.scan, SCAN_MENU[period])
    else:
        yield from bps.mv(lockin.fast_target, "X", lockin.fast_period, period, lockin.fast_duration, 0)
        yield from bps.mv(lockin.fast_active, 1)


def aux_profile(lockin, aux_index: int, profile, duration_s: float, update_s: float = 0.1):
    """Drive AuxOut<aux_index> (front-panel numbering, 1..) along profile(t) for duration_s; ends
    exactly on profile(duration_s)."""
    sig = getattr(lockin, f"aux_out{aux_index}")
    t0 = time.monotonic()
    while True:
        t = time.monotonic() - t0
        last = t >= duration_s
        yield from bps.mv(sig, float(profile(duration_s if last else t)))
        if last:
            return
        yield from bps.checkpoint()
        yield from bps.sleep(max(0.0, min(update_s - (time.monotonic() - t0 - t), duration_s - t)))


def aux_ramp(lockin, aux_index: int, start_v: float, stop_v: float, rate_v_per_s: float, update_s: float = 0.1):
    if rate_v_per_s <= 0:
        raise ValueError("rate must be > 0")
    duration = abs(stop_v - start_v) / rate_v_per_s
    sign = 1.0 if stop_v >= start_v else -1.0
    yield from aux_profile(lockin, aux_index, lambda t: start_v + sign * rate_v_per_s * min(t, duration),
                           duration, update_s)


def aux_waveform(lockin, aux_index: int, points, update_s: float = 0.05):
    """Piecewise-linear AuxOut through [[t_s, volts], ...]."""
    pts = sorted((float(t), float(v)) for t, v in points)
    if len(pts) < 2 or pts[0][0] < 0:
        raise ValueError("need >= 2 points with t >= 0")

    def profile(t):
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t <= t1:
                return v0 if t1 == t0 else v0 + (v1 - v0) * (max(t, t0) - t0) / (t1 - t0)
        return pts[-1][1]

    yield from aux_profile(lockin, aux_index, profile, pts[-1][0], update_s)
