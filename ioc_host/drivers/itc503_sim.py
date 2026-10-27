"""Simulated ITC503 for `ioc_host/iocs/itc503_ioc.py --mock`: same
attributes as PyMeasure's ITC503 (+ raw ask/write for X, H, M), first-order
thermal model while the heater is auto, sweep ramps run `time_scale` times
faster than real time. Manual heater / gas values act on the model too."""
from __future__ import annotations

import math
import random
import time


class MockITC503:
    version = "ITC503 Version 1.10 (c) OXFORD 1997 SIMULATOR"

    def __init__(self, start_k: float = 10.0, time_scale: float = 60.0, tau_s: float = 20.0) -> None:
        self.time_scale = time_scale
        self.tau_s = tau_s
        self._t = start_k
        self._sp = start_k
        self._ramp = None  # (from, to, t0, duration_s)
        self._pending = None
        self._last = time.monotonic()
        self.heater_gas_mode = "AUTO"
        self.control_mode = "RU"
        self._gas = 20.0
        self._heater_manual = 0.0
        self.proportional_band, self.integral_action_time, self.derivative_action_time = 5.0, 1.0, 0.0
        self.auto_pid = False
        self.heater_sensor = 1
        self.max_volts = 40.0
        self.front_panel_display = "temperature 1"

    def _check_remote(self) -> None:
        if self.control_mode not in ("RL", "RU"):
            raise RuntimeError("ITC503 in local mode: command refused")

    def _setpoint_now(self) -> float:
        if self._ramp is None:
            return self._sp
        a, b, t0, dur = self._ramp
        frac = min((time.monotonic() - t0) / dur, 1.0) if dur > 0 else 1.0
        if frac >= 1.0:
            self._ramp, self._sp = None, b
            return b
        return a + (b - a) * frac

    def _heater_auto(self) -> bool:
        return self.heater_gas_mode in ("AM", "AUTO")

    def _step(self) -> None:
        now = time.monotonic()
        dt, self._last = (now - self._last) * self.time_scale, now
        if self._heater_auto():
            goal = self._setpoint_now()
        else:  # manual heater: equilibrium rises with the heater output above a 4 K bath
            goal = 4.2 + 3.0 * self._heater_manual
        self._t += (goal - self._t) * (1 - math.exp(-dt / self.tau_s))

    @property
    def temperature_1(self) -> float:
        self._step()
        return round(self._t + random.gauss(0, 1e-4), 5)

    @property
    def temperature_2(self) -> float:
        return round(self._t + 0.8, 4)

    @property
    def temperature_3(self) -> float:
        return round(self._t + 0.9, 4)

    @property
    def temperature_error(self) -> float:
        return round(self._setpoint_now() - self._t, 4)

    @property
    def temperature_setpoint(self) -> float:
        return round(self._setpoint_now(), 4)

    @temperature_setpoint.setter
    def temperature_setpoint(self, value: float) -> None:
        self._check_remote()
        self._ramp, self._sp = None, float(value)

    @property
    def heater(self) -> float:
        if not self._heater_auto():
            return self._heater_manual
        return max(0.0, min(99.9, 5.0 * (self._setpoint_now() - self._t) + 10.0))

    @heater.setter
    def heater(self, value: float) -> None:
        self._check_remote()
        self._heater_manual = max(0.0, min(99.9, float(value)))

    @property
    def heater_voltage(self) -> float:
        return round(self.heater / 100 * self.max_volts, 2)

    @property
    def gasflow(self) -> float:
        if self.heater_gas_mode in ("MA", "AUTO"):
            return max(0.0, min(99.9, 20.0 - 2.0 * (self._setpoint_now() - self._t)))
        return self._gas

    @gasflow.setter
    def gasflow(self, value: float) -> None:
        self._check_remote()
        self._gas = max(0.0, min(99.9, float(value)))

    @property
    def sweep_status(self) -> int:
        self._setpoint_now()
        return 1 if self._ramp else 0

    @sweep_status.setter
    def sweep_status(self, value: int) -> None:
        if value and self._pending:          # 1 = start the programmed ramp
            target, duration = self._pending
            self._ramp = (self._setpoint_now(), target, time.monotonic(), duration)
        elif not value and self._ramp:       # 0 = stop, hold the current setpoint
            self._sp, self._ramp = self._setpoint_now(), None

    def program_sweep(self, targets, times_min, holds_min) -> None:
        self._check_remote()
        self._pending = (float(targets[0]), float(times_min[0]) * 60.0 / self.time_scale)

    def ask(self, cmd: str) -> str:
        if cmd == "X":
            return (f"X0A{('MANUAL', 'AM', 'MA', 'AUTO').index(self.heater_gas_mode)}"
                    f"C{('LL', 'RL', 'LU', 'RU').index(self.control_mode)}S{self.sweep_status:02d}"
                    f"H{self.heater_sensor}L{int(self.auto_pid)}")
        raise RuntimeError(f"?{cmd}")

    def write(self, cmd: str) -> None:
        self._check_remote()
        if cmd[0] == "H" and cmd[1:] in ("1", "2", "3"):
            self.heater_sensor = int(cmd[1:])
        elif cmd[0] == "M":
            self.max_volts = float(cmd[1:])
        else:
            raise RuntimeError(f"?{cmd}")
