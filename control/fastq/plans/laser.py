"""Carbide laser plan stubs - the building blocks of laser_sequence_scan.

  safe_state(laser, lockin)   shutter closed, PP off, energy Internal, burst 1, lock-in AuxOut1 = 0
  reference(...)              shutter closed, just record for a while
  pulses(...)                 N shots (single pulse or exact-count burst), shutter closed between
  ramp(...)                   free-running train, energy follows a lock-in AuxOut1 ramp

`mark(phase)` is a callback plan that records the current phase (see
measurements.laser_sequence_scan); pass None to skip.
"""
from __future__ import annotations

from bluesky import plan_stubs as bps

SHUTTER_SETTLE_S = 0.3   # readback vs. true optical blocking margin
PP_TIMEOUT_S = 10.0

# Temporary interlocks for a single run: laser_sequence_scan(laser_limits={...})
# sets them for that run only (and records them in the run metadata); outside
# such a run no limits apply. A step exceeding them is refused before the run starts.
NO_LIMITS = {"max_attenuator_percent": 100.0, "min_pp_divider": 1.0}
LIMITS = dict(NO_LIMITS)


def set_limits(limits: dict | None) -> None:
    """Replace the active limits (None = no limits)."""
    limits = limits or {}
    unknown = set(limits) - set(NO_LIMITS)
    if unknown:
        raise ValueError(f"unknown laser limits {sorted(unknown)} (known: {sorted(NO_LIMITS)})")
    LIMITS.clear()
    LIMITS.update(NO_LIMITS)
    LIMITS.update({k: float(v) for k, v in limits.items()})


def check_limits(attenuator_percent=None, divider=None) -> None:
    if attenuator_percent is not None and attenuator_percent > LIMITS["max_attenuator_percent"]:
        raise ValueError(f"attenuator {attenuator_percent} % above the run limit "
                         f"{LIMITS['max_attenuator_percent']} % (laser_limits)")
    if divider is not None and divider < LIMITS["min_pp_divider"]:
        raise ValueError(f"pulse-picker divider {divider} below the run limit "
                         f"{LIMITS['min_pp_divider']:.0f} (laser_limits)")


def _ensure_min_divider(laser):
    """Keep the pulse picker at >= the lab minimum divider."""
    current = yield from bps.rd(laser.pp_divider)
    if current < LIMITS["min_pp_divider"]:
        yield from bps.mv(laser.pp_divider, int(LIMITS["min_pp_divider"]))


def _mark(mark, phase):
    if mark is not None:
        yield from mark(phase)


def safe_state(laser, lockin=None, aux_index: int = 1):
    """Every step attempted even if one fails (used as the finalizer)."""
    errors = []
    for sig, value in ((laser.shutter_open, 0), (laser.pp_enable, 0), (laser.energy_control, "Internal"),
                       (laser.burst_count, 1)) + (((getattr(lockin, f"aux_out{aux_index}"), 0.0),) if lockin else ()):
        try:
            yield from bps.mv(sig, value)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{sig.name}: {exc}")
    if errors:
        raise RuntimeError("laser safe state incomplete: " + "; ".join(errors))


def close_shutter(laser, settle: bool = True):
    yield from bps.mv(laser.shutter_open, 0)
    if settle:
        yield from bps.sleep(SHUTTER_SETTLE_S)


def set_pulse_duration(laser, fs: float | None):
    if fs is None:  # keep whatever the laser is set to
        return
    if abs((yield from bps.rd(laser.pulse_duration)) - fs) > laser.pulse_duration.tolerance:
        yield from bps.mv(laser.pulse_duration, fs)


def _poll_period(laser) -> float:
    scan = laser.scan.get()
    try:
        return float(str(scan).split()[0])
    except ValueError:
        return 0.5


def _wait_pp_off(laser, expected_s: float, timeout_s: float):
    # never accept an "off" readback from before the enable: wait >= 1.5 readback periods first
    yield from bps.sleep(1.5 * _poll_period(laser) + expected_s)
    waited = 0.0
    while (yield from bps.rd(laser.pp_enable)):
        if waited > timeout_s:
            raise TimeoutError("pulse picker did not switch itself off (burst not confirmed)")
        yield from bps.sleep(0.1)
        waited += 0.1


def reference(laser, duration_s: float, phase: str = "reference", mark=None):
    yield from close_shutter(laser)
    if duration_s > 0:
        yield from _mark(mark, phase)
        yield from bps.sleep(duration_s)


def pulses(laser, duration_fs: float, attenuator_percent: float, pulse_count: int = 1, pulse_delay_s: float = 0.0,
           mode: str = "single", burst_divider: int = 1000, burst_pulse_count: int = 100,
           phase: str = "pulses", delay_phase: str = "pulse_delay", burst_timeout_s: float = 120.0, mark=None):
    """pulse_count shots, pulse_delay_s apart with the shutter closed. A shot
    is one pulse (single) or an exact burst counted by the laser (burst)."""
    if mode not in ("single", "burst"):
        raise ValueError("mode must be 'single' or 'burst'")
    check_limits(attenuator_percent, burst_divider if mode == "burst" else None)
    yield from _ensure_min_divider(laser)
    yield from bps.mv(laser.attenuator, attenuator_percent)
    yield from set_pulse_duration(laser, duration_fs)
    yield from close_shutter(laser, settle=False)
    yield from bps.mv(laser.burst_count, 1)
    for shot in range(1, int(pulse_count) + 1):
        yield from _mark(mark, phase)
        yield from bps.mv(laser.shutter_open, 1)
        if mode == "single":
            yield from bps.mv(laser.pp_enable, 1)
            yield from _wait_pp_off(laser, 0.0, PP_TIMEOUT_S)
        else:
            f_hz = max((yield from bps.rd(laser.output_frequency)) * 1000.0, 1.0)
            yield from bps.mv(laser.burst_count, int(burst_pulse_count), laser.pp_divider, int(burst_divider))
            yield from bps.mv(laser.pp_enable, 1)
            yield from _wait_pp_off(laser, burst_pulse_count * burst_divider / f_hz, burst_timeout_s)
            yield from bps.mv(laser.burst_count, 1)
        yield from close_shutter(laser)
        if shot < pulse_count and pulse_delay_s > 0:
            yield from _mark(mark, delay_phase)
            yield from bps.sleep(pulse_delay_s)


def ramp(laser, lockin, duration_fs: float, start_percent: float, stop_percent: float, divider: int,
         rate_v_per_s: float, phase: str = "ramp", aux_index: int = 1, full_scale_v: float = 10.0,
         update_s: float = 0.1, mark=None):
    """Free-running train at `divider`, pulse energy following the lock-in
    AuxOut (External energy control) swept start% -> stop%. AuxOut is set to
    the start value BEFORE External is switched on - never a stale level live."""
    from fastq.plans.lockin import aux_ramp

    for p in (start_percent, stop_percent):
        if not 0.0 <= p <= 100.0:
            raise ValueError(f"{p} % out of 0..100")
    check_limits(max(start_percent, stop_percent), divider)
    aux = getattr(lockin, f"aux_out{aux_index}")
    start_v, stop_v = start_percent / 100 * full_scale_v, stop_percent / 100 * full_scale_v
    yield from close_shutter(laser, settle=False)
    yield from set_pulse_duration(laser, duration_fs)
    yield from bps.mv(aux, 0.0)
    yield from bps.mv(aux, start_v)
    yield from bps.mv(laser.energy_control, "External")
    yield from bps.mv(laser.burst_count, 0, laser.pp_divider, int(divider))

    def body():
        yield from bps.mv(laser.shutter_open, 1)
        yield from bps.mv(laser.pp_enable, 1)
        yield from _mark(mark, phase)
        yield from aux_ramp(lockin, aux_index, start_v, stop_v, rate_v_per_s, update_s)

    def cleanup():
        yield from bps.mv(laser.pp_enable, 0)
        yield from close_shutter(laser, settle=False)
        yield from bps.mv(laser.energy_control, "Internal", laser.burst_count, 1)

    from bluesky.preprocessors import finalize_wrapper

    yield from finalize_wrapper(body(), cleanup())
