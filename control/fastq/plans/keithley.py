"""Keithley 6221 + 2182A plan stubs (ioc_host/iocs/keithley_ioc.py): mode configuration and
recording a sweep's points while it runs. Used by measurements.iv_curves, the transport recipes
and the R(T) recipes - one implementation each."""
from __future__ import annotations

from bluesky import plan_stubs as bps
from ophyd import Signal

LIVE_PERIOD_S = 1.0   # sweep points are recorded this often while the sweep runs
_IV = {k: Signal(name=k, value=0.0) for k in ("curve", "current_a", "voltage_v", "t_s", "temperature_k")}


def configure_delta(k, current_a: float, count: int, delay_s: float, compliance_v: float, nplc: float | None = None):
    """Delta mode: +/- current_a, `count` readings (0 = continuous until Stop)."""
    args = [k.mode, "Delta", k.compliance, compliance_v, k.delta_high, current_a, k.delta_low, -current_a,
            k.delta_delay, delay_s, k.delta_count, count]
    if nplc is not None:
        args += [k.nplc, nplc]
    yield from bps.mv(*args)


def configure_pulse_delta(k, current_a: float, low_a: float, width_s: float, source_delay_s: float, count: int,
                          interval_plc: int, compliance_v: float):
    yield from bps.mv(k.mode, "PulseDelta", k.compliance, compliance_v, k.pd_high, current_a, k.pd_low, low_a,
                      k.pd_width, width_s, k.pd_source_delay, source_delay_s, k.pd_count, count,
                      k.pd_interval_plc, interval_plc)


def emit_iv(k, itc, curve: int, start: int = 0):
    """Rows start.. of the running/finished sweep as events of the "iv" stream; returns the number
    of rows emitted so far. The IOC publishes the points measured so far (N last, so N rows exist)."""
    n = int((yield from bps.rd(k.n)))
    if n <= start:
        return start
    currents = yield from bps.rd(k.currents)
    volts = yield from bps.rd(k.readings)
    times = yield from bps.rd(k.timestamps)
    n = min(n, len(currents), len(volts), len(times))
    temp = (yield from bps.rd(itc.temp)) if itc is not None else float("nan")
    for i in range(start, n):
        for key, value in (("curve", curve), ("current_a", currents[i]), ("voltage_v", volts[i]),
                           ("t_s", times[i]), ("temperature_k", temp)):
            _IV[key].put(float(value))
        yield from bps.create("iv")
        for sig in _IV.values():
            yield from bps.read(sig)
        yield from bps.save()
    return max(n, start)


def record_sweep(k, itc, curve: int):
    """Run one sweep (trigger = Acquire) and record its points live in the "iv" stream, then one
    summary event in "primary" (Keithley + temperature controller)."""
    status = yield from bps.trigger(k, group=f"sweep{curve}")
    emitted = 0
    while not status.done:
        emitted = yield from emit_iv(k, itc, curve, emitted)
        yield from bps.checkpoint()      # a deferred pause takes effect within ~1 s
        yield from bps.sleep(LIVE_PERIOD_S)
    yield from bps.wait(group=f"sweep{curve}")   # raises if the sweep failed
    yield from emit_iv(k, itc, curve, emitted)
    yield from bps.create("primary")
    for det in [k] + ([itc] if itc is not None else []):
        yield from bps.read(det)
    yield from bps.save()
