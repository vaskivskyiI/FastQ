"""IOC: Oxford Instruments ITC503 temperature controller (GPIB, PyMeasure).

    python ioc_host/iocs/itc503_ioc.py --gpib GPIB0::23::INSTR --prefix FASTQ:ITC1:
    python ioc_host/iocs/itc503_ioc.py --mock --prefix TEST:ITC1:

Commands (ITC503 handbook, ISOBUS chapter; PyMeasure ITC503): R0-R10 reads
(setpoint, sensors 1-3, error, heater %/V, gas, P/I/D), X status, T setpoint,
A heater/gas auto-manual, C local/remote, O heater %, G gas %, P/I/D, L auto-PID,
H heater sensor, M heater voltage limit (V), F front-panel display, sweep table
x/y/s + S. Not implemented: auto-PID table (p/q), target-voltage table (t/v),
gas-flow configuration (c/d/m/n/o) - expert settings.

Safety: startup only reads. Remote mode is requested right before a write
(the ITC503 answers reads in local mode); the heater mode is never changed
at startup (it once was, on every IOC restart). A setpoint put stops a running
sweep first - the ITC ignores setpoints while it sweeps.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

HEATER_GAS_MODES = ("MANUAL", "AM", "MA", "AUTO")
CONTROL_MODES = ("LL", "RL", "LU", "RU")
DISPLAY = ("temperature setpoint", "temperature 1", "temperature 2", "temperature 3", "temperature error",
           "heater", "heater voltage", "gasflow", "proportional band", "integral action time",
           "derivative action time", "channel 1 freq/4", "channel 2 freq/4", "channel 3 freq/4")


def _get(attr: str):
    return lambda s: getattr(s.drv, attr)


class ITC503(Instrument):
    TYPE = "itc503"
    DESCRIPTION = "Oxford Instruments ITC503 temperature controller (GPIB)"
    DEFAULT_PREFIX = "FASTQ:ITC1:"
    POLL = 2.0

    Temp = Param(float, units="K", precision=4, get=_get("temperature_1"), poll=1.0, kind="hinted",
                 overview=True, trend=True, doc="sensor 1 (control) temperature")
    Temp2 = Param(float, units="K", precision=4, get=_get("temperature_2"), doc="sensor 2")
    Temp3 = Param(float, units="K", precision=4, get=_get("temperature_3"), doc="sensor 3")
    TempError = Param(float, units="K", precision=4, get=_get("temperature_error"),
                      doc="setpoint - control sensor temperature (R4)")
    Setpoint = Param(float, units="K", precision=3, limits=(0.0, 500.0), get=_get("temperature_setpoint"),
                     set="write_setpoint", overview=True, refresh=["SweepStep", "Ramping", "HeaterGasMode"],
                     positioner={"readback": "Temp", "tolerance": 0.05, "settle_s": 20.0, "timeout_s": 3600.0},
                     doc="temperature setpoint; set() completes when |Temp - setpoint| <= tolerance for settle_s")
    Heater = Param(float, units="%", precision=1, get=_get("heater"), overview=True, doc="heater output (% of limit)")
    HeaterVolts = Param(float, units="V", precision=2, get=_get("heater_voltage"), doc="heater output voltage (R6)")
    GasFlow = Param(float, units="%", precision=1, get=_get("gasflow"), doc="gas flow (needle valve)")
    SweepStep = Param(int, get=_get("sweep_status"), doc="sweep table step, 0 = no sweep running")
    Ramping = Param(bool, get=lambda s: int(s.drv.sweep_status or 0) != 0,
                    doc="a ramp (sweep) is running - same name as every temperature controller")
    HeaterGasMode = Param("enum", choices=HEATER_GAS_MODES, get=_get("heater_gas_mode"), set="write_mode",
                          kind="config", doc="heater/gas manual-auto (AM = heater auto, gas manual)")
    ControlMode = Param("enum", choices=CONTROL_MODES, get=_get("control_mode"),
                        set=lambda s, v: setattr(s.drv, "control_mode", v), kind="config",
                        doc="local/remote, locked/unlocked (L* = front panel only: writes are refused)")
    HeaterManual = Param(float, units="%", precision=1, limits=(0.0, 99.9), set="write_heater", refresh=["Heater"],
                         doc="heater output in heater-manual mode (O); no effect while the heater is auto")
    GasManual = Param(float, units="%", precision=1, limits=(0.0, 99.9), set="write_gas", refresh=["GasFlow"],
                      doc="needle valve in gas-manual mode (G); no effect while the gas is auto")
    P = Param(float, units="K", precision=2, limits=(0.0, 1677.7), poll=10.0, get=_get("proportional_band"),
              set="write_pid_p", kind="config", doc="proportional band (ignored while AutoPID = 1)")
    I = Param(float, units="min", precision=2, limits=(0.0, 140.0), poll=10.0, get=_get("integral_action_time"),
              set="write_pid_i", kind="config", doc="integral action time")
    D = Param(float, units="min", precision=2, limits=(0.0, 273.0), poll=10.0, get=_get("derivative_action_time"),
              set="write_pid_d", kind="config", doc="derivative action time")
    AutoPID = Param(bool, poll=10.0, get=_get("auto_pid"), set="write_auto_pid", kind="config",
                    doc="1 = PID from the controller's auto-PID table (L1)")
    # protected: the heater then follows another sensor - if that one is not on the sample stage the
    # stage overheats while the loop "sees" nothing
    HeaterSensor = Param(int, limits=(1, 3), poll=10.0, get="read_heater_sensor", set="write_heater_sensor",
                         kind="config", safety="protected", doc="sensor the heater loop controls (H)")
    # protected: raises the heater power that can reach the sample; the ITC has no read command for it
    HeaterMaxV = Param(float, units="V", precision=1, limits=(0.0, 40.0), set="write_heater_max",
                       kind="config", safety="protected",
                       doc="heater voltage limit (M; 0 = dynamic). Write-only: shows the last value put here")
    Display = Param("enum", choices=DISPLAY, set="write_display", kind="omitted", safety="safe",
                    doc="front-panel display (F). Write-only")
    Version = Param(str, poll=0, get=_get("version"), kind="config", doc="firmware version (V)")
    RampTarget = Param(float, units="K", precision=3, writable=True, kind="config", safety="safe",
                       doc="ramp end temperature")
    RampTime = Param(float, units="min", precision=2, writable=True, initial=1.0, limits=(0.1, 1339.9),
                     kind="config", safety="safe", doc="ramp duration")
    RampHold = Param(float, units="min", precision=2, writable=True, initial=0.0, limits=(0.0, 1339.9),
                     kind="config", safety="safe", doc="hold time at RampTarget (sweep table step 1)")
    AutoHeater = Param(bool, writable=True, initial=1, kind="config", safety="safe",
                       doc="1: Setpoint/RampStart switch the heater to auto (gas mode kept); 0: never touch modes")
    RampStart = Action("ramp_start", doc="ramp the setpoint to RampTarget over RampTime",
                       refresh=["SweepStep", "Ramping", "Setpoint", "HeaterGasMode"])
    # safe: stopping a sweep only holds the present setpoint
    RampStop = Action("ramp_stop", safety="safe", doc="stop the sweep, hold the present setpoint",
                      refresh=["SweepStep", "Ramping", "Setpoint"])

    ophyd = {"stop": "RampStop"}

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--gpib", help="VISA address, e.g. GPIB0::23::INSTR")
        parser.add_argument("--mock-start-k", type=float, default=10.0)
        parser.add_argument("--mock-time-scale", type=float, default=60.0, help="simulated s per real s")
        parser.add_argument("--tolerance", type=float, default=0.05, help="setpoint reached within this (K)")
        parser.add_argument("--settle-s", type=float, default=20.0, help="... held this long (s)")

    def instance_schema(self) -> dict:
        schema = self.schema()
        for p in schema["params"]:
            if p["name"] == "Setpoint":
                p["positioner"] = {**p["positioner"], "tolerance": self.args.tolerance, "settle_s": self.args.settle_s}
        return schema

    def open(self) -> None:
        if self.mock:
            from ioc_host.drivers.itc503_sim import MockITC503

            self.drv = MockITC503(self.args.mock_start_k, self.args.mock_time_scale)
        else:
            if not self.args.gpib:
                raise ValueError("--gpib is required (or --mock)")
            from pymeasure.instruments.oxfordinstruments import ITC503 as Driver

            self.drv = Driver(self.args.gpib)  # only a GPIB device clear, no settings change

    def _remote(self) -> None:
        if self.drv.control_mode not in ("RL", "RU"):
            self.drv.control_mode = "RU"

    def _heater_auto(self) -> None:
        if not self.values["AutoHeater"]:
            return
        mode = self.drv.heater_gas_mode
        target = "AUTO" if mode in ("MA", "AUTO") else "AM"  # keep the gas mode
        if mode != target:
            self.drv.heater_gas_mode = target

    def write_setpoint(self, value: float) -> None:
        self._remote()
        if int(self.drv.sweep_status or 0):
            self.drv.sweep_status = 0
        self.drv.temperature_setpoint = value
        self._heater_auto()

    def write_mode(self, mode: str) -> None:
        self._remote()
        self.drv.heater_gas_mode = mode

    def _set(self, attr: str, value) -> None:
        self._remote()
        setattr(self.drv, attr, value)

    def write_heater(self, v: float) -> None:
        self._set("heater", v)

    def write_gas(self, v: float) -> None:
        self._set("gasflow", v)

    def write_pid_p(self, v: float) -> None:
        self._set("proportional_band", v)

    def write_pid_i(self, v: float) -> None:
        self._set("integral_action_time", v)

    def write_pid_d(self, v: float) -> None:
        self._set("derivative_action_time", v)

    def write_auto_pid(self, v: bool) -> None:
        self._set("auto_pid", bool(v))

    def write_display(self, v: str) -> None:
        self._set("front_panel_display", v)

    def read_heater_sensor(self) -> int:
        return int(self.drv.ask("X")[10])  # X0A0C3S00H1L0: digit after H

    def write_heater_sensor(self, n: int) -> None:
        self._remote()
        self.drv.write(f"H{int(n)}")  # not a PyMeasure property

    def write_heater_max(self, v: float) -> None:
        self._remote()
        self.drv.write(f"M{v:.1f}")

    def ramp_start(self) -> None:
        self._remote()
        self.drv.program_sweep([float(self.values["RampTarget"])], [float(self.values["RampTime"])],
                               [float(self.values["RampHold"])])
        self.drv.sweep_status = 1
        self._heater_auto()

    def ramp_stop(self) -> None:
        self._remote()
        self.drv.sweep_status = 0


if __name__ == "__main__":
    run_instrument(ITC503)
