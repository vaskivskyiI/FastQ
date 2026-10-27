"""Scan-rate control for polled IOCs.

Normal rate: the standard EPICS .SCAN field of the polled record
(`caput FASTQ:MFLI1:X.SCAN ".1 second"`). caproto's use_scan_field scan loop
re-reads it every cycle.

.SCAN stops at ".1 second". For faster, temporary sampling (e.g. the lock-in
at 50 Hz during a laser shot) these PVs override the loop period:
  FastPollTarget     name of the polled PV (empty = the IOC's only one)
  FastPollPeriodS    period while active (s), may be < 0.1
  FastPollDurationS  auto-revert after this many s (0 = until stopped)
  FastPollActive     1 = start, 0 = stop now; on stop the rate returns to
                     whatever .SCAN says at that moment
"""
from __future__ import annotations

import asyncio
import logging

from caproto.server import pvproperty

from ioc_host.common.ioc_base import IOCBase, pv_bool

logger = logging.getLogger(__name__)

_IDLE = ("Passive", "Event", "I/O Intr")


def _scan_seconds(field_inst) -> float:
    raw = field_inst.scan_rate.value
    choice = raw if isinstance(raw, str) else field_inst.scan_rate.enum_strings[raw]
    return 0.0 if choice in _IDLE else float(choice.split(" ")[0])


class FastPollGroup(IOCBase):
    FastPollTarget = pvproperty(value="", dtype=str)
    FastPollPeriodS = pvproperty(value=0.02, precision=4, units="s")
    FastPollDurationS = pvproperty(value=0.0, precision=3, units="s")
    FastPollActive = pvproperty(value=0, dtype=bool)

    def _init_fast_poll(self, periods: dict[str, float]) -> None:
        """periods: {polled PV attribute: startup period in s}."""
        self._fp_periods = dict(periods)
        self._fp_target: str | None = None
        self._fp_timer: asyncio.Task | None = None
        for name, period in periods.items():
            getattr(self, name).field_inst._scan_rate_sec = period

    def _field(self, name: str):
        return getattr(self, name).field_inst

    @FastPollActive.startup
    async def FastPollActive(self, instance, async_lib):
        # show the real startup rate in .SCAN (caproto leaves it "Passive")
        for name, period in self._fp_periods.items():
            f = self._field(name)
            for choice in f.scan_rate.enum_strings:
                if choice not in _IDLE and abs(float(choice.split(" ")[0]) - period) < 1e-9:
                    await f.scan_rate.write(choice)

    async def _fp_stop(self, reason: str) -> None:
        if self._fp_target is not None:
            f = self._field(self._fp_target)
            f._scan_rate_sec = _scan_seconds(f) or self._fp_periods[self._fp_target]
            logger.info("fast poll off for %s (%s)", self._fp_target, reason)
        self._fp_target = None
        if self._fp_timer is not None:
            self._fp_timer.cancel()
            self._fp_timer = None

    async def _fp_revert_later(self, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        self._fp_timer = None
        await self._fp_stop("duration elapsed")
        await self.FastPollActive.write(0)

    @FastPollActive.putter
    async def FastPollActive(self, instance, value):
        if not pv_bool(value):
            await self._fp_stop("stopped")
            return 0
        target = (self.FastPollTarget.value or "").strip()
        if not target and len(self._fp_periods) == 1:
            target = next(iter(self._fp_periods))
        if target not in self._fp_periods:
            raise ValueError(f"FastPollTarget must be one of {sorted(self._fp_periods)}")
        period = float(self.FastPollPeriodS.value)
        if period <= 0:
            raise ValueError("FastPollPeriodS must be > 0")
        await self._fp_stop("retarget")
        self._field(target)._scan_rate_sec = period
        self._fp_target = target
        duration = float(self.FastPollDurationS.value)
        if duration > 0:
            self._fp_timer = asyncio.ensure_future(self._fp_revert_later(duration))
        logger.info("fast poll on for %s: %.4g s%s", target, period, f" for {duration:g} s" if duration > 0 else "")
        return 1
