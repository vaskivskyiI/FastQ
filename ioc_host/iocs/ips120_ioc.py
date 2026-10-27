"""IOC: Oxford Instruments IPS120-10 superconducting magnet power supply
(GPIB or RS232 via PyMeasure IPS120_10; ISOBUS commands R/X/I/J/S/T/A/H/C/M).

    python ioc_host/iocs/ips120_ioc.py --resource GPIB0::25::INSTR --prefix FDM:IPS:
    python ioc_host/iocs/ips120_ioc.py --resource ASRL5::INSTR --prefix FDM:IPS:
    python ioc_host/iocs/ips120_ioc.py --mock --prefix TEST:IPS:

Commands (IPS120-10 handbook, ISOBUS chapter; PyMeasure IPS120_10; Lab::Measurement
IPS.pm): R0 output current, R1 voltage, R2 measured magnet current, R5/R6 target
current / current sweep rate, R7/R8/R9 output field / target field / field sweep
rate, R15 voltage limit, R16-R19 persistent current, trip current, persistent
field, trip field, R20 switch heater current, R21/R22 safe current limits, R23 lead
resistance, R24 inductance. X = XmnAnCnHnMmnPmn (system, limits, activity,
control, switch heater, mode + sweep, polarity). Not implemented: H2 (switch heater
on WITHOUT the current check - can quench the magnet), Q (protocol), P polarity
commands (PS120 only), the front-panel set-up (trip/limit) values - read only here.

Startup only reads (no device clear either). Writes switch the supply to
remote (C3) first. A put of FieldSetpoint writes J and the activity "To
setpoint" (A1): the leads sweep at SweepRate and the positioner waits for the
output field (Field) and Sweeping = 0. With the switch heater off (persistent
mode) that moves the leads only - MagnetField stays at the persistent field.
The switch heater is changed only by HeaterOn/HeaterOff and by the
EnterPersistent/LeavePersistent sequences (PyMeasure's procedure).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

ACTIVITY = ("Hold", "To setpoint", "To zero", "Clamp")
ACTIVITY_CODE = (0, 1, 2, 4)  # A4 = clamp (manual; 4dmag ips.py had A3)
SWEEP = ("At rest", "Sweeping", "Sweep limiting", "Sweeping+limiting")
CONTROL = ("Local locked", "Remote locked", "Local unlocked", "Remote unlocked")
HEATER = {0: "Off (magnet at zero)", 1: "On", 2: "Off (magnet at field)", 5: "Fault: heater current low",
          8: "No switch fitted"}
SYSTEM = {0: "Normal", 1: "Quenched", 2: "Over heated", 4: "Warming up", 8: "Fault"}
LIMITS = {0: "Normal", 1: "On positive voltage limit", 2: "On negative voltage limit",
          4: "Outside negative current limit", 8: "Outside positive current limit"}
MODE = {0: "Amps, fast", 1: "Tesla, fast", 4: "Amps, slow", 5: "Tesla, slow"}
UNITS = ("Amps", "Tesla")  # M8 / M9: display units, sweep speed (fast/slow) unaffected


class Driver:
    """PyMeasure's reply checking and retries; the raw ISOBUS commands are used
    because some of its properties read the wrong register (current_measured = R1 = voltage)."""

    def __init__(self, resource: str, timeout_ms: int = 3000) -> None:
        from pymeasure.instruments.oxfordinstruments import IPS120_10

        self.ips = IPS120_10(resource, clear_buffer=False, timeout=timeout_ms, write_termination="\r")

    def ask(self, cmd: str) -> str:
        return self.ips.ask(cmd)

    def write(self, cmd: str) -> None:
        self.ips.write(cmd)

    def close(self):
        self.ips.adapter.close()


class SimIPS:
    """The supply's ISOBUS replies. Field = current x tesla_per_amp; sweeps run
    time_scale x fast (default 60: the rate in T/min acts per second). H1 is refused
    unless the output current equals the persistent current, as the real supply does."""

    def __init__(self, time_scale: float = 60.0, tesla_per_amp: float = 0.1) -> None:
        self.k, self.scale = tesla_per_amp, time_scale
        self.out, self.target, self.rate, self.pers = 0.0, 0.0, 0.1, 0.0  # output/target/persistent in tesla
        self.activity, self.control, self.heater, self.mode, self.t = 0, 0, 0, 1, time.monotonic()

    def _update(self):
        now = time.monotonic()
        dt, self.t = (now - self.t) * self.scale / 60.0, now
        goal = {1: self.target, 2: 0.0}.get(self.activity)
        if goal is not None:
            d, step = goal - self.out, self.rate * dt
            self.out = goal if abs(d) <= step else self.out + step * (1 if d > 0 else -1)
        if self.heater == 1:
            self.pers = self.out

    def _sweeping(self):
        goal = {1: self.target, 2: 0.0}.get(self.activity)
        return int(goal is not None and abs(goal - self.out) > 1e-6)

    def ask(self, cmd: str) -> str:
        self._update()
        if cmd == "X":
            h = self.heater if self.heater == 1 else (2 if abs(self.pers) > 1e-6 else 0)
            return f"X00A{self.activity}C{self.control}H{h}M{self.mode}{self._sweeping()}P00"
        if cmd == "V":
            return "IPS120-10  Version 3.07  (c) OXFORD 1996 (SIMULATED)"
        n, k = int(cmd[1:]), self.k
        magnet = self.out if self.heater == 1 else self.pers
        val = {0: self.out / k, 1: 0.002 * self.out / k + (0.5 if self._sweeping() else 0.0), 2: magnet / k,
               5: self.target / k, 6: self.rate / k, 7: self.out, 8: self.target, 9: self.rate, 15: 5.0,
               16: self.pers / k, 17: 0.0, 18: self.pers, 19: 0.0, 20: 40.0, 21: -100.0, 22: 100.0,
               23: 12.0, 24: 20.0}[n]
        return f"{val:+.4f}"

    def write(self, cmd: str) -> None:
        self._update()
        c, v = cmd[0], cmd[1:]
        if c != "C" and self.control not in (1, 3):
            raise RuntimeError(f"?{cmd} (local mode)")
        if c == "C":
            self.control = int(v)
        elif c == "J":
            self.target = float(v)
        elif c == "I":
            self.target = float(v) * self.k
        elif c == "T":
            self.rate = float(v)
        elif c == "S":
            self.rate = float(v) * self.k
        elif c == "A":
            self.activity = int(v)
        elif c == "M":
            self.mode = int(v) - 8 + (self.mode & 4)
        elif c == "H":
            if int(v) == 1 and self.heater != 1 and abs(self.out - self.pers) > 1e-4:
                raise RuntimeError("?H1 (output current differs from the persistent current)")
            self.heater = 1 if int(v) in (1, 2) else 0


class IPS120(Instrument):
    TYPE = "ips120"
    DESCRIPTION = "Oxford IPS120-10 superconducting magnet power supply (field sweeps, switch heater, persistent mode)"
    DEFAULT_PREFIX = "FDM:IPS:"
    POLL = 1.0

    Field = Param(float, units="T", precision=4, get=lambda s: s.num("R7"), kind="hinted", overview=True, trend=True,
                  doc="output (leads) field = magnet field while the switch heater is on")
    MagnetField = Param(float, units="T", precision=4, get="magnet_field", overview=True,
                        doc="field in the magnet: output field with heater on, persistent field with heater off")
    # confirm (not protected): bounded by --field-range, which is the magnet's protection
    FieldSetpoint = Param(float, units="T", precision=4, limits=(-20.0, 20.0), get=lambda s: s.num("R8"),
                          set="go_to_field", refresh=["Sweeping", "Activity"],
                          positioner={"readback": "Field", "moving": "Sweeping", "tolerance": 0.0005, "settle_s": 2.0,
                                      "timeout_s": 7200.0},
                          doc="target field; a put starts the sweep (activity To setpoint)")
    # confirm: bounded by --max-rate (the quench-safe rate belongs on the command line, not in a PV)
    SweepRate = Param(float, units="T/min", precision=4, limits=(0.0, 10.0), get=lambda s: s.num("R9"), poll=5.0,
                      set="set_rate", kind="config", doc="field sweep rate")
    CurrentSetpoint = Param(float, units="A", precision=3, get=lambda s: s.num("R5"), poll=5.0, set="go_to_current",
                            refresh=["Sweeping", "Activity"],
                            doc="target current (I); a put starts the sweep. Refused outside the safe current limits")
    # protected: no IOC-side limit exists for the current rate - too fast quenches the magnet
    CurrentSweepRate = Param(float, units="A/min", precision=3, get=lambda s: s.num("R6"), poll=5.0,
                             set="set_current_rate", kind="config", safety="protected", doc="current sweep rate (S)")
    Activity = Param("enum", choices=ACTIVITY, get=lambda s: ACTIVITY[ACTIVITY_CODE.index(s.status(4))],
                     set="set_activity", kind="config", overview=True,
                     doc="Hold | To setpoint | To zero | Clamp (output clamped)")
    Sweeping = Param(bool, get=lambda s: s.status(11) != 0, kind="omitted", doc="the output is sweeping")
    SweepStatus = Param("enum", choices=SWEEP, get=lambda s: SWEEP[s.status(11) & 3], kind="omitted")
    Mode = Param(str, get=lambda s: MODE.get(s.status(10), f"code {s.status(10)}"), poll=5.0, kind="config",
                 doc="display units and sweep speed (fast/slow, front panel)")
    DisplayUnits = Param("enum", choices=UNITS, writable=True, set=lambda s, v: s._remote(f"M{8 + UNITS.index(v)}"),
                         kind="omitted", safety="safe", refresh=["Mode"],
                         doc="front-panel display units (M8/M9; sweep speed unchanged). Write-only")
    SwitchHeater = Param(str, get=lambda s: HEATER.get(s.status(8), f"code {s.status(8)}"), overview=True,
                         doc="persistent-switch heater (On = switch open, magnet current follows the leads)")
    Persistent = Param(bool, get=lambda s: s.status(8) == 2, overview=True,
                       doc="1 = persistent mode (heater off, magnet at field)")
    PersistentField = Param(float, units="T", precision=4, get=lambda s: s.num("R18"), poll=2.0)
    PersistentCurrent = Param(float, units="A", precision=3, get=lambda s: s.num("R16"), poll=2.0, kind="config")
    OutputCurrent = Param(float, units="A", precision=3, get=lambda s: s.num("R0"), doc="demand (output) current")
    MagnetCurrent = Param(float, units="A", precision=3, get=lambda s: s.num("R2"), doc="measured magnet current")
    Voltage = Param(float, units="V", precision=3, get=lambda s: s.num("R1"), poll=2.0, doc="power supply voltage")
    System = Param(str, get=lambda s: SYSTEM.get(s.status(1), f"code {s.status(1)}"), poll=2.0, overview=True,
                   doc="Normal | Quenched | Over heated | Warming up | Fault")
    Quenched = Param(bool, get=lambda s: s.status(1) == 1, poll=2.0, overview=True, event="On",
                     doc="1 = the magnet quenched")
    Limits = Param(str, get=lambda s: LIMITS.get(s.status(2), f"code {s.status(2)}"), poll=2.0,
                   doc="voltage / current limit state")
    Control = Param("enum", choices=CONTROL, get=lambda s: CONTROL[s.status(6)], poll=5.0, kind="config",
                    doc="front-panel lock (writes switch to Remote unlocked)")
    VoltageLimit = Param(float, units="V", precision=2, get=lambda s: s.num("R15"), poll=0, kind="config",
                         doc="software voltage limit (front panel)")
    TripCurrent = Param(float, units="A", precision=3, get=lambda s: s.num("R17"), poll=10.0, kind="config",
                        doc="current at the last quench")
    TripField = Param(float, units="T", precision=4, get=lambda s: s.num("R19"), poll=10.0, kind="config",
                      doc="field at the last quench")
    SwitchHeaterCurrent = Param(float, units="mA", precision=1, get=lambda s: s.num("R20"), poll=0, kind="config")
    SafeCurrentNeg = Param(float, units="A", precision=3, get=lambda s: s.num("R21"), poll=0, kind="config",
                           doc="safe current limit, most negative (front panel)")
    SafeCurrentPos = Param(float, units="A", precision=3, get=lambda s: s.num("R22"), poll=0, kind="config",
                           doc="safe current limit, most positive (front panel)")
    LeadResistance = Param(float, units="mOhm", precision=2, get=lambda s: s.num("R23"), poll=0, kind="config")
    Inductance = Param(float, units="H", precision=2, get=lambda s: s.num("R24"), poll=0, kind="config")
    Version = Param(str, get=lambda s: s.drv.ask("V"), poll=0, kind="config")

    # protected: the switch heater decides whether the magnet current can change - wrong use quenches it
    HeaterOn = Action("heater_on", safety="protected", refresh=["SwitchHeater", "Persistent", "MagnetField"],
                      doc="switch heater on (H1: the IPS refuses unless output current = persistent current)")
    HeaterOff = Action("heater_off", safety="protected", refresh=["SwitchHeater", "Persistent", "MagnetField"],
                       doc="switch heater off (H0): the magnet keeps its current (persistent mode)")
    EnterPersistent = Action("enter_persistent", safety="protected", exclusive=True, timeout_s=7200.0,
                             doc="at rest: hold, heater off, wait --switch-wait-s, ramp the leads to zero")
    LeavePersistent = Action("leave_persistent", safety="protected", exclusive=True, timeout_s=7200.0,
                             doc="ramp the leads to the persistent field, hold, heater on, wait --switch-wait-s")
    GoToZero = Action(lambda s: s.set_activity("To zero"), refresh=["Activity", "Sweeping"],
                      doc="sweep the output to zero (A2)")
    # safe: stopping a sweep never endangers the magnet
    Hold = Action(lambda s: s.set_activity("Hold"), safety="safe", refresh=["Activity", "Sweeping"],
                  doc="stop the sweep (activity Hold)")
    Abort = Action("abort", safety="safe", lock=False,
                   doc="stop a running EnterPersistent/LeavePersistent sequence (the output is put on Hold)")

    ophyd = {"stop": "Hold"}

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--resource", default="GPIB0::25::INSTR", help="VISA resource (GPIB or ASRL, 9600 8N2)")
        parser.add_argument("--field-range", type=float, default=7.0, help="|FieldSetpoint| limit (T)")
        parser.add_argument("--max-rate", type=float, default=1.0, help="SweepRate limit (T/min)")
        parser.add_argument("--tolerance", type=float, default=0.0005, help="positioner: |Field - setpoint| (T)")
        parser.add_argument("--switch-wait-s", type=float, default=None,
                            help="switch heater warm-up / cool-down wait (s; default 20, PyMeasure; mock 2)")
        parser.add_argument("--mock-time-scale", type=float, default=60.0, help="simulated s per real s")

    def __init__(self, args) -> None:
        super().__init__(args)
        self._x, self._x_t = "", 0.0
        self._abort = threading.Event()

    def instance_schema(self) -> dict:
        schema = self.schema()
        for p in schema["params"]:
            if p["name"] == "FieldSetpoint":
                p["limits"] = [-self.args.field_range, self.args.field_range]
                p["positioner"] = {**p["positioner"], "tolerance": self.args.tolerance}
            elif p["name"] == "SweepRate":
                p["limits"] = [0.0, self.args.max_rate]
        return schema

    def open(self) -> None:
        self.drv = SimIPS(self.args.mock_time_scale) if self.mock else Driver(self.args.resource)
        self._x_t = 0.0

    def num(self, cmd: str) -> float:
        return float(self.drv.ask(cmd))

    def status(self, i: int) -> int:
        """digit i of the X status string XmnAnCnHnMmnPmn (cached 0.3 s: one X per poll round)."""
        if time.monotonic() - self._x_t > 0.3:
            x = self.drv.ask("X")
            if len(x) < 15 or x[0] != "X":
                raise ValueError(f"IPS: bad status reply {x!r}")
            self._x, self._x_t = x, time.monotonic()
        return int(self._x[i])

    def magnet_field(self) -> float:
        return self.num("R7") if self.status(8) == 1 else self.num("R18")

    def _remote(self, cmd: str) -> None:
        self.drv.write("C3")
        self.drv.write(cmd)
        self._x_t = 0.0

    def go_to_field(self, value: float) -> None:
        if abs(value) > self.args.field_range:
            raise ValueError(f"|{value}| T > --field-range {self.args.field_range}")
        self._remote(f"J{value:.4f}")
        self._remote("A1")

    def go_to_current(self, value: float) -> None:
        lo, hi = self.num("R21"), self.num("R22")
        if not lo <= value <= hi:
            raise ValueError(f"{value} A outside the safe current limits [{lo}, {hi}] A")
        field = value * self.num("R7") / self.num("R0") if abs(self.num("R0")) > 1e-3 else None
        if field is not None and abs(field) > self.args.field_range:
            raise ValueError(f"{value} A ~ {field:.3f} T > --field-range {self.args.field_range}")
        self._remote(f"I{value:.3f}")
        self._remote("A1")

    def set_rate(self, value: float) -> None:
        if not 0 < value <= self.args.max_rate:
            raise ValueError(f"SweepRate {value} outside (0, {self.args.max_rate}]")
        self._remote(f"T{value:.4f}")

    def set_current_rate(self, value: float) -> None:
        if value <= 0:
            raise ValueError("CurrentSweepRate must be > 0")
        self._remote(f"S{value:.3f}")

    def set_activity(self, value: str) -> None:
        self._remote(f"A{ACTIVITY_CODE[ACTIVITY.index(value)]}")

    def heater_on(self) -> None:
        self._remote("H1")

    def heater_off(self) -> None:
        self._remote("H0")

    # --- persistent-mode sequences (PyMeasure enable/disable_persistent_mode) ---
    def abort(self) -> None:
        self._abort.set()

    def _publish_state(self) -> None:
        self.publish(Field=self.num("R7"), MagnetField=self.magnet_field(), Sweeping=self.status(11) != 0,
                     SwitchHeater=HEATER.get(self.status(8), f"code {self.status(8)}"),
                     Persistent=self.status(8) == 2, OutputCurrent=self.num("R0"))

    def _wait(self, seconds: float | None = None) -> None:
        """wait for 'at rest' (seconds=None) or a fixed time, publishing the state; Abort -> Hold."""
        t_end = None if seconds is None else time.monotonic() + seconds
        while True:
            if self._abort.is_set():
                self._remote("A0")
                raise RuntimeError("aborted - output on Hold")
            self._x_t = 0.0
            self._publish_state()
            if t_end is None and self.status(11) == 0:
                return
            if t_end is not None and time.monotonic() >= t_end:
                return
            time.sleep(0.5)

    def _switch_wait(self) -> float:
        return self.args.switch_wait_s if self.args.switch_wait_s is not None else (2.0 if self.mock else 20.0)

    def enter_persistent(self) -> None:
        self._abort.clear()
        if self.status(11) != 0:
            raise RuntimeError("magnet not at rest - wait for the sweep to end")
        if self.status(8) != 1:
            return  # heater already off
        self._remote("A0")
        self._remote("H0")
        self._wait(self._switch_wait())
        self._remote("A2")
        self._wait()

    def leave_persistent(self) -> None:
        self._abort.clear()
        if self.status(11) != 0:
            raise RuntimeError("magnet not at rest - wait for the sweep to end")
        if self.status(8) == 1:
            return  # heater already on
        field = self.num("R18")
        if abs(field) > self.args.field_range:
            raise ValueError(f"persistent field {field} T > --field-range {self.args.field_range}")
        self._remote(f"J{field:.4f}")
        self._remote("A1")
        self._wait()
        self._remote("A0")
        self._remote("H1")
        self._wait(self._switch_wait())


if __name__ == "__main__":
    run_instrument(IPS120)
