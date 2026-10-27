"""Queue flow: conditions and repetition for unattended measurements (no data runs of their own).

    wait_until("22:00")                                   then the next queue item starts at 22:00
    wait_for("itc1.temp", within=[4.2, 0.05], for_s=600)  T within 4.2 +- 0.05 K for 10 min
    wait_for("vac.pressure1", below=1e-5, timeout_s=7200) vacuum good (fails after 2 h)
    log_until("itc1.temp", below=4.5, readables=["@lockin"], period_s=10)
                                                          a cooldown curve (a run), done at 4.5 K
    sequence([{"plan": "tm_delta", "kwargs": {"points": 20}}, {"plan": "wait_for", "kwargs": {...}}],
             repeats=0)                                   every step its own run, until Stop

All of them checkpoint every second: Pause / Stop / Abort act at once, a deferred pause between steps.
"""
from __future__ import annotations

import datetime as dt
import time

from bluesky import plan_stubs as bps
from bluesky import preprocessors as bpp

POLL_S = 1.0


def wait_until(time_hhmm: str = "22:00", date: str | None = None):
    """Wait until a local time (HH:MM, today - or tomorrow if it has passed; date YYYY-MM-DD to fix the day)."""
    hh, mm = (int(x) for x in time_hhmm.split(":"))
    now = dt.datetime.now()
    day = dt.date.fromisoformat(date) if date else now.date()
    at = dt.datetime.combine(day, dt.time(hh, mm))
    if at <= now and not date:
        at += dt.timedelta(days=1)
    print(f"waiting until {at:%Y-%m-%d %H:%M}")
    while dt.datetime.now() < at:
        yield from bps.checkpoint()
        yield from bps.sleep(min(POLL_S, max(0.0, (at - dt.datetime.now()).total_seconds())))


def _condition(signal, above, below, within):
    """(signal object, ok(value)) of a wait condition."""
    from fastq.session import resolve

    sig = resolve(signal)
    if getattr(sig, "_pos_readback", None):  # a positioner device: its readback (the temperature, not the setpoint)
        sig = getattr(sig, sig._pos_readback)
    elif hasattr(sig, "component_names"):
        raise ValueError(f"{signal}: a device - give its value as device.attr (e.g. itc1.temp)")
    if above is None and below is None and within is None:
        raise ValueError("give above, below or within=[target, tolerance]")

    def ok(v: float) -> bool:
        return ((above is None or v > above) and (below is None or v < below)
                and (within is None or abs(v - within[0]) <= within[1]))
    return sig, ok


def _held(ok, for_s: float):
    """ok over time: True once the condition has held continuously for for_s."""
    since = None

    def check(v: float) -> bool:
        nonlocal since
        if not ok(v):
            since = None
            return False
        since = since or time.monotonic()
        return time.monotonic() - since >= for_s
    return check


def wait_for(signal: str, above: float | None = None, below: float | None = None, within: list | None = None,
             for_s: float = 0.0, timeout_s: float | None = None):
    """Wait until a value meets the condition continuously for for_s: above / below a limit, or
    within=[target, tolerance]. signal: "device.attr" (itc1.temp), a device (its readback) or "@role".
    timeout_s: give up (the queue stops - a night does not run on with a broken condition)."""
    sig, ok = _condition(signal, above, below, within)
    done = _held(ok, for_s)
    t0 = time.monotonic()
    while True:
        v = float((yield from bps.rd(sig)))
        if done(v):
            print(f"{getattr(sig, 'name', signal)} = {v:g}: condition met")
            return
        if timeout_s is not None and time.monotonic() - t0 > timeout_s:
            raise TimeoutError(f"{getattr(sig, 'name', signal)} = {v:g}: condition not met within {timeout_s:g} s")
        yield from bps.checkpoint()
        yield from bps.sleep(POLL_S)


def log_until(signal: str, above: float | None = None, below: float | None = None, within: list | None = None,
              for_s: float = 0.0, readables: list = (), period_s: float = 10.0, timeout_s: float | None = None,
              md: dict | None = None):
    """Record `signal` and `readables` every period_s until the condition holds for for_s (as in
    wait_for) - a cooldown / warm-up curve, a pump-down, a drift log - then the next queue item
    starts. One run (exit status success when the condition is met); timeout_s: the run fails."""
    from fastq.session import resolve

    sig, ok = _condition(signal, above, below, within)
    done = _held(ok, for_s)
    others = [resolve(r) for r in readables]
    names = [getattr(o, "name", str(o)) for o in others]
    if not any(o is sig or getattr(sig, "parent", None) is o for o in others):
        others.insert(0, sig)  # (a readable device that owns the signal reads it already)
    cond = {k: v for k, v in (("above", above), ("below", below), ("within", within)) if v is not None}
    meta = {"run_kind": "log_until", "detectors": [], "readables": names, "period_s": period_s,
            "condition": {"signal": getattr(sig, "name", str(signal)), **cond, "for_s": for_s},
            "plot_hints": {"x": "time", "signals": [sig.name], "group": None, "average": False, "map_y": None},
            **(md or {})}

    @bpp.run_decorator(md=meta)
    def inner():
        t0 = time.monotonic()
        while True:
            t_next = time.monotonic() + period_s
            reading = yield from bps.trigger_and_read(others)
            v = float(reading[sig.name]["value"])
            if done(v):
                print(f"{sig.name} = {v:g}: condition met")
                return
            if timeout_s is not None and time.monotonic() - t0 > timeout_s:
                raise TimeoutError(f"{sig.name} = {v:g}: condition not met within {timeout_s:g} s")
            while time.monotonic() < t_next:  # pausable while waiting for the next point
                yield from bps.checkpoint()
                yield from bps.sleep(min(POLL_S, max(0.0, t_next - time.monotonic())))
    return (yield from inner())


def sequence(steps: list, repeats: int = 1, pause_s: float = 0.0):
    """Run queued-plan steps [{"plan": name, "kwargs": {...}, "comment": "..."}, ...] in order,
    `repeats` times (0 = until Stop). Every step is the plan exactly as queued alone (its own run,
    metadata, ELOG entry). pause_s between steps."""
    from fastq.plan_registry import REGISTRY

    for s in steps:
        if s.get("plan") not in REGISTRY:
            raise ValueError(f"unknown plan {s.get('plan')!r} in the sequence (plans: reload them first?)")
    n = 0
    while repeats == 0 or n < repeats:
        n += 1
        for i, s in enumerate(steps, 1):
            print(f"sequence pass {n}{'' if repeats == 0 else f'/{repeats}'}, step {i}/{len(steps)}: {s['plan']}")
            kwargs = dict(s.get("kwargs") or {})
            comment = s.get("comment") or ""
            if comment:
                kwargs["comment"] = comment
            yield from REGISTRY[s["plan"]](**kwargs)
            yield from bps.checkpoint()
            if pause_s:
                yield from bps.sleep(pause_s)
