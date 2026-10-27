"""Temperature plan stubs for any temperature controller whose IOC declares a
Setpoint positioner and RampTarget / RampTime / RampStart / RampStop (itc503, mercury_itc).

    stabilize(itc, 4.0)                              setpoint + wait until stable
    sweep(itc, [[10, 2, 0.5]])                       ramp(s), K/min
    sweep(itc, segs, during=lambda: bps.trigger_and_read([lockin, itc]))
"""
from __future__ import annotations

import time

from bluesky import plan_stubs as bps

ITC_MIN_RAMP_MIN, ITC_MAX_RAMP_MIN = 0.1, 1339.9


def stabilize(itc, setpoint_k: float, tolerance_k: float = 0.05, settle_s: float = 20.0):
    """Go to setpoint_k and wait until |T - setpoint| <= tolerance for settle_s
    (the IOC stops a running ramp first)."""
    yield from bps.abs_set(itc, setpoint_k, tolerance=tolerance_k, settle_s=settle_s, wait=True)


def _wait(check, during, poll_s, timeout_s: float, what: str):
    t0 = time.monotonic()
    while not (yield from check()):
        if time.monotonic() - t0 > timeout_s:
            raise TimeoutError(f"{what} not reached after {timeout_s:.0f} s (heater/gas mode, heater range?)")
        yield from bps.checkpoint()
        if during is not None:
            yield from during()
        else:
            yield from bps.sleep(poll_s)


def sweep(itc, segments, final_tolerance_k: float = 0.05, go_to_start: bool = True,
          start_tolerance_k: float = 0.2, start_settle_s: float = 20.0, during=None, poll_s: float = 1.0,
          final_timeout_s: float = 3600.0):
    """Ramp through segments [[from_K, to_K, K_per_min], ...] (continuous),
    each as one ITC ramp; the next starts when the setpoint readback reaches
    the segment end. Finishes when T is within final_tolerance_k of the last
    target. `during`: plan factory run repeatedly meanwhile (e.g. a reading). A segment fails when its
    setpoint is not reached 1.5x its ramp time + 10 min after the start, the end when T is not
    within final_tolerance_k after final_timeout_s."""
    segs = [(float(a), float(b), float(r)) for a, b, r in segments]
    for (_, b, _), (a2, _, _) in zip(segs, segs[1:]):
        if abs(b - a2) > 1e-9:
            raise ValueError(f"segments not continuous at {b} K / {a2} K")
    if any(r <= 0 for _, _, r in segs):
        raise ValueError("ramp rates must be > 0 K/min")
    if go_to_start:
        yield from stabilize(itc, segs[0][0], start_tolerance_k, start_settle_s)
    for a, b, rate in segs:
        minutes = min(max(abs(b - a) / rate, ITC_MIN_RAMP_MIN), ITC_MAX_RAMP_MIN)
        yield from bps.mv(itc.ramp_target, b, itc.ramp_time, minutes)
        yield from bps.mv(itc.ramp_start_cmd, 1)

        def reached(target=b):
            sp = yield from bps.rd(itc.setpoint)
            return abs(sp - target) <= 1e-3

        yield from _wait(reached, during, poll_s, 1.5 * minutes * 60 + 600, f"ramp setpoint {b} K")
    final = segs[-1][1]

    def arrived():
        t = yield from bps.rd(itc.temp)
        return abs(t - final) <= final_tolerance_k

    yield from _wait(arrived, during, poll_s, final_timeout_s, f"temperature {final} K")


def stop_sweep(itc):
    yield from bps.mv(itc.ramp_stop_cmd, 1)
