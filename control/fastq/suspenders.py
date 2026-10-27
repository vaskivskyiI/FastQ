"""Automatic pause / resume of runs (Bluesky suspenders) from lab.yaml:

    suspenders:
      - {signal: interlock.triggered, when: true, resume_after_s: 10, why: laser interlock tripped}
      - {signal: vac.pressure1, above: 1.0e-4, resume_below: 5.0e-5, resume_after_s: 30}
      - {signal: flow.flow, below: 0.1, why: no HHG gas}

While a condition holds, the running plan is held at its next checkpoint (the point is
measured again afterwards); it continues resume_after_s after the condition cleared.
Signals are `device.attribute` of discovered devices; an unavailable one is skipped with
a warning (a partly powered lab still runs). The reason is shown in the dashboard and
logged in the console.
"""
from __future__ import annotations

import logging

from bluesky.suspenders import SuspendBoolHigh, SuspendBoolLow, SuspendCeil, SuspendFloor

log = logging.getLogger("fastq.suspenders")
ACTIVE: list = []


def _note(why: str, tripped: bool) -> None:
    from fastq import live

    live.note_suspension(why if tripped else None)


class _Noted:
    """Mixin: tell the dashboard (live file) when a suspender trips / clears."""

    def _should_suspend(self, value):
        trip = super()._should_suspend(value)
        if trip:
            _note(self._why, True)
        return trip

    def _should_resume(self, value):
        ok = super()._should_resume(value)
        if ok:
            _note(self._why, False)
        return ok


def _cls(base):
    return type(f"Noted{base.__name__}", (_Noted, base), {})


def install(RE, lab: dict) -> list:
    from fastq.session import device_by_name

    for s in list(ACTIVE):
        RE.remove_suspender(s)
        ACTIVE.remove(s)
    for spec in lab.get("suspenders") or []:
        try:
            sig = device_by_name(spec["signal"])
        except Exception as exc:  # noqa: BLE001
            log.warning("suspender on %s not installed: %s", spec.get("signal"), exc)
            continue
        why = spec.get("why") or f"{spec['signal']} {' '.join(f'{k}={v}' for k, v in spec.items() if k in ('when', 'above', 'below'))}"
        kw = {"sleep": float(spec.get("resume_after_s", 5.0)), "tripped_message": why}
        if "above" in spec:
            s = _cls(SuspendCeil)(sig, float(spec["above"]), resume_thresh=spec.get("resume_below"), **kw)
        elif "below" in spec:
            s = _cls(SuspendFloor)(sig, float(spec["below"]), resume_thresh=spec.get("resume_above"), **kw)
        else:
            s = _cls(SuspendBoolHigh if spec.get("when", True) else SuspendBoolLow)(sig, **kw)
        s._why = why
        RE.install_suspender(s)
        ACTIVE.append(s)
        log.info("suspender: %s", why)
    return ACTIVE
