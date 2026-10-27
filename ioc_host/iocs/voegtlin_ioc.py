"""IOC: Voegtlin red-y smart mass flow controller (Modbus RTU over RS-485/USB).

    python ioc_host/iocs/voegtlin_ioc.py --port COM27 --prefix FASTQ:FLOW1:
    python ioc_host/iocs/voegtlin_ioc.py --mock --prefix TEST:FLOW1:

Register map: red-y smart series operating instructions part II, digital
communication (329-3042, smart_digi_com_E1_5, serial numbers from 110 000):
flow 0x0000, temperature 0x0002, setpoint 0x0006, analog input 0x0008, valve
signal 0x000a, alarms 0x000c, hardware errors 0x000d, control function 0x000e,
ramp 0x000f, serial 0x001e, software version 0x0021, PID set 0x0035, reset
hardware errors 0x404f, setpoint save mode 0x4050, gas table (LUT) 0x4139, range
0x6020, fluid name 0x6042, unit 0x6046, totalisers 0x6380/0x6382, totaliser unit
0x6386. Not implemented: analog I/O configuration, PID parameter values, power-up
alarm, baud rate / address, soft reset (installation settings, front software).

The setpoint is only used in control function Automatic (0) or Digital setpoint
(1); a Setpoint put in another mode is refused rather than silently ignored.
Fully opening the valve (control function 21/23 or a manual valve signal) is a
separate, protected command.
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

REG = {"flow": 0x0000, "temp": 0x0002, "setpoint": 0x0006, "analog_in": 0x0008, "valve": 0x000A,
       "alarms": 0x000C, "hw_errors": 0x000D, "control": 0x000E, "ramp": 0x000F, "serial": 0x001E,
       "sw_version": 0x0021, "pid_select": 0x0035, "reset_errors": 0x404F, "save_mode": 0x4050,
       "lut": 0x4139, "range": 0x6020, "fluid": 0x6042, "unit": 0x6046, "total1": 0x6380, "total2": 0x6382,
       "total_unit": 0x6386}
# control function register 0x000e: code -> name (manual p. 18)
CONTROL = {0: "Automatic", 1: "Digital setpoint", 2: "Analog setpoint", 10: "Valve manual", 20: "Setpoint 0%",
           21: "Setpoint 100%", 22: "Valve closed", 23: "Valve fully open", 30: "Test analog output",
           31: "Test DAC"}
CONTROL_MODES = tuple(CONTROL.values())
CONTROL_CODE = {v: k for k, v in CONTROL.items()}
UNSAFE_CONTROL = ("Valve manual", "Setpoint 100%", "Valve fully open", "Test analog output", "Test DAC")
PID_SETS = ("User 1", "User 2", "Fast (U)", "Medium (V)", "Slow (W)")
ALARM_BITS = {0: "negative flow", 1: "backflow over threshold", 15: "hardware error"}
HW_ERROR_BITS = {0: "power-up alarm", 1: "analog setpoint out of range", 2: "zero point / leakage",
                 3: "no gas / jammed valve", 4: "no reaction", 5: "sensor communication", 7: "EEPROM access",
                 10: "current input overload", 11: "sensor does not match gas data"}


def _bits(value: int, names: dict) -> str:
    return ", ".join(n for b, n in names.items() if value >> b & 1) or "none"


class Modbus:
    """Thin minimalmodbus wrapper with retries (the bus drops single frames)."""

    def __init__(self, port: str, address: int, retries: int = 5) -> None:
        import minimalmodbus
        import serial

        self.inst = minimalmodbus.Instrument(port, address)
        self.inst.serial.baudrate, self.inst.serial.bytesize = 9600, 8
        self.inst.serial.parity, self.inst.serial.stopbits = serial.PARITY_NONE, 2
        self.inst.serial.timeout = 0.5
        self.retries = retries

    def _retry(self, func, *args, **kwargs):
        for i in range(self.retries):
            try:
                return func(*args, **kwargs)
            except (OSError, ValueError, IOError):
                if i == self.retries - 1:
                    raise
                time.sleep(0.05)

    def read_float(self, reg):
        return self._retry(self.inst.read_float, reg, functioncode=3)

    def write_float(self, reg, value):
        return self._retry(self.inst.write_float, reg, value)

    def read_register(self, reg):
        return self._retry(self.inst.read_register, reg, functioncode=3)

    def write_register(self, reg, value):
        return self._retry(self.inst.write_register, reg, int(value))

    def read_long(self, reg):
        return self._retry(self.inst.read_long, reg, functioncode=3, signed=False)

    def read_string(self, reg, nregs=4):
        return self._retry(self.inst.read_string, reg, nregs, functioncode=3).replace("\x00", "").strip()

    def close(self):
        self.inst.serial.close()


class SimModbus:
    """A GSC controller for 0-5 ln/min air: regulates in Automatic/Digital, follows the valve
    overrides, integrates the totalisers, flags 'no gas' when the valve is open with no flow."""

    def __init__(self) -> None:
        self.f = {REG["flow"]: 0.0, REG["temp"]: 22.0, REG["setpoint"]: 0.0, REG["analog_in"]: 0.0,
                  REG["valve"]: 0.0, REG["range"]: 5.0, REG["total1"]: 0.0, REG["total2"]: 1234.5}
        self.r = {REG["control"]: 0, REG["ramp"]: 0, REG["alarms"]: 0, REG["hw_errors"]: 0, REG["pid_select"]: 0,
                  REG["save_mode"]: 1, REG["lut"]: 2, REG["sw_version"]: 0x0437}
        self.s = {REG["fluid"]: "Air", REG["unit"]: "ln/min", REG["total_unit"]: "ln"}
        self.buffer_valve, self._t = 0.0, time.monotonic()

    def _update(self):
        now = time.monotonic()
        dt, self._t = now - self._t, now
        mode, rng = self.r[REG["control"]], self.f[REG["range"]]
        target = {0: self.f[REG["setpoint"]], 1: self.f[REG["setpoint"]], 2: 0.0, 10: rng * self.buffer_valve / 100,
                  20: 0.0, 21: rng, 22: 0.0, 23: rng * 1.2, 30: 0.0, 31: 0.0}[mode]
        self.f[REG["flow"]] += 0.5 * (target - self.f[REG["flow"]])
        self.f[REG["valve"]] = {10: self.buffer_valve, 22: 0.0, 23: 100.0}.get(mode, min(100.0, 100 * target / rng))
        total = max(self.f[REG["flow"]], 0.0) * dt / 60.0  # ln/min -> ln
        self.f[REG["total1"]] += total
        self.f[REG["total2"]] += total

    def read_float(self, reg):
        self._update()
        if reg == REG["flow"]:
            return self.f[reg] + random.gauss(0, 0.002)
        return self.f[reg]

    def write_float(self, reg, value):
        if reg == REG["valve"]:
            self.buffer_valve = float(value)  # becomes active in control function 10
        elif reg in (REG["setpoint"], REG["total1"]):
            self.f[reg] = float(value)
        else:
            raise ValueError(f"simulator: register 0x{reg:04x} is not writable")

    def read_register(self, reg):
        self._update()
        return self.r[reg]

    def write_register(self, reg, value):
        if reg == REG["reset_errors"]:
            self.r[REG["hw_errors"]] &= ~int(value)
        elif reg == REG["control"] and int(value) not in CONTROL:
            raise ValueError(f"illegal control function {value}")
        else:
            self.r[reg] = int(value)

    def read_long(self, reg):
        return 110123

    def read_string(self, reg, nregs=4):
        return self.s[reg]


class Voegtlin(Instrument):
    TYPE = "voegtlin"
    DESCRIPTION = "Voegtlin red-y mass flow controller (gas flow to the HHG gas cell)"
    DEFAULT_PREFIX = "FASTQ:FLOW1:"

    Flow = Param(float, units="ln/min", precision=4, get=lambda s: s.drv.read_float(REG["flow"]),
                 kind="hinted", overview=True, trend=True, doc="measured gas flow (unit: see Unit)")
    Temperature = Param(float, units="C", precision=2, get=lambda s: s.drv.read_float(REG["temp"]), poll=5.0,
                        overview=True, doc="gas temperature (sensor self-heating: slightly above the inlet)")
    Setpoint = Param(float, units="ln/min", precision=4, limits=(0.0, 100.0),
                     get=lambda s: s.drv.read_float(REG["setpoint"]), set="write_setpoint",
                     positioner={"readback": "Flow", "tolerance": 0.01, "settle_s": 2.0, "timeout_s": 60.0},
                     overview=True, doc="flow setpoint (used in control Automatic / Digital setpoint; "
                                        "refused above Range and in other modes)")
    AnalogInput = Param(float, precision=3, poll=5.0, get=lambda s: s.drv.read_float(REG["analog_in"]),
                        kind="config", doc="analog setpoint input (V or mA)")
    ValveValue = Param(float, units="%", precision=2, get=lambda s: s.drv.read_float(REG["valve"]), kind="config",
                       doc="valve control signal now")
    Control = Param("enum", choices=CONTROL_MODES, get=lambda s: CONTROL[s.drv.read_register(REG["control"])],
                    set="write_control", kind="config", refresh=["ValveValue"],
                    doc="control function (0x000e). Valve manual / fully open / Setpoint 100% / test modes are "
                        "refused here: use ValvePosition or OpenValve")
    # protected: a manual valve signal can open the valve fully
    ValvePosition = Param(float, units="%", precision=1, limits=(0.0, 100.0), set="write_valve", safety="protected",
                          refresh=["Control", "ValveValue"],
                          doc="manual valve signal: written, then control function Valve manual (10). Write-only")
    RampMs = Param(int, units="ms", limits=(0, 10000), get=lambda s: s.drv.read_register(REG["ramp"]), poll=0,
                   set="write_ramp", kind="config", safety="safe",
                   doc="setpoint ramp time, 0 = off, else 200..10000 ms")
    Alarms = Param(str, poll=2.0, get=lambda s: _bits(s.drv.read_register(REG["alarms"]), ALARM_BITS),
                   overview=True, doc="alarm bits (0x000c)")
    HardwareErrors = Param(str, poll=2.0, get=lambda s: _bits(s.drv.read_register(REG["hw_errors"]), HW_ERROR_BITS),
                           overview=True, doc="hardware error bits (0x000d; persist until ResetErrors)")
    GasTable = Param(int, limits=(2, 11), poll=0, get=lambda s: s.drv.read_register(REG["lut"]),
                     set=lambda s, v: s.drv.write_register(REG["lut"], v), kind="config",
                     refresh=["Gas", "Unit", "Range"],
                     doc="calibration data set (gas) 2..11 (LUT select; tables are made by the manufacturer)")
    Gas = Param(str, poll=0, get=lambda s: s.drv.read_string(REG["fluid"]), kind="config", doc="gas of the table")
    Unit = Param(str, poll=0, get=lambda s: s.drv.read_string(REG["unit"]), kind="config", doc="flow unit")
    Range = Param(float, precision=3, poll=0, get=lambda s: s.drv.read_float(REG["range"]), kind="config",
                  doc="full scale of the gas table (flow unit)")
    PIDSet = Param("enum", choices=PID_SETS, poll=0, get=lambda s: s.drv.read_register(REG["pid_select"]),
                   set=lambda s, v: s.drv.write_register(REG["pid_select"], PID_SETS.index(v)), kind="config",
                   doc="control parameter set (fast / medium / slow response)")
    SetpointSaveMode = Param("enum", choices=("Manual", "Automatic"), poll=0, kind="config",
                             get=lambda s: s.drv.read_register(REG["save_mode"]),
                             set=lambda s, v: s.drv.write_register(REG["save_mode"], v == "Automatic"),
                             doc="Automatic stores every setpoint in EEPROM (1e6 write cycles) - "
                                 "Manual for scans that change the setpoint often")
    Totalizer = Param(float, precision=3, poll=5.0, get=lambda s: s.drv.read_float(REG["total1"]),
                      doc="gas used since ResetTotalizer (unit: TotalizerUnit)")
    TotalizerAll = Param(float, precision=3, poll=10.0, get=lambda s: s.drv.read_float(REG["total2"]),
                         kind="config", doc="gas used in the instrument's life (not resettable)")
    TotalizerUnit = Param(str, poll=0, get=lambda s: s.drv.read_string(REG["total_unit"]), kind="config")
    Serial = Param(int, poll=0, get=lambda s: s.drv.read_long(REG["serial"]), kind="config")
    Firmware = Param(str, poll=0, kind="config", get=lambda s: "{}.{}.{}".format(
        *(lambda v: (v >> 8, v >> 4 & 15, v & 15))(s.drv.read_register(REG["sw_version"]))))
    CloseValve = Action(lambda s: s.drv.write_register(REG["control"], CONTROL_CODE["Valve closed"]),
                        doc="close the valve (no flow; control function 22)", refresh=["Control", "Flow"])
    # protected: full flow into the gas cell / chamber
    OpenValve = Action(lambda s: s.drv.write_register(REG["control"], CONTROL_CODE["Valve fully open"]),
                       safety="protected", refresh=["Control", "Flow"],
                       doc="open the valve fully (control function 23)")
    ResetTotalizer = Action(lambda s: s.drv.write_float(REG["total1"], 0.0), refresh=["Totalizer"],
                            doc="restart the resettable totaliser at 0")
    ResetErrors = Action("reset_errors", refresh=["HardwareErrors", "Alarms"],
                         doc="clear the hardware error bits that are set now (0x404f)")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--port", default="COM27", help="serial port")
        parser.add_argument("--address", type=int, default=247, help="Modbus slave address")

    def open(self) -> None:
        self.drv = SimModbus() if self.mock else Modbus(self.args.port, self.args.address)

    def write_setpoint(self, value: float) -> None:
        mode = CONTROL.get(self.drv.read_register(REG["control"]), "?")
        if mode not in ("Automatic", "Digital setpoint"):
            raise ValueError(f"control function is {mode!r}: the setpoint would be ignored - "
                             "set Control to Digital setpoint first")
        rng = self.drv.read_float(REG["range"])
        if value > rng:
            raise ValueError(f"setpoint {value} > range {rng}")
        self.drv.write_float(REG["setpoint"], value)

    def write_control(self, mode: str) -> None:
        if mode in UNSAFE_CONTROL:
            raise ValueError(f"{mode!r} refused here - use ValvePosition / OpenValve (protected)")
        self.drv.write_register(REG["control"], CONTROL_CODE[mode])

    def write_valve(self, pct: float) -> None:
        self.drv.write_float(REG["valve"], pct)  # buffered until control function 10
        self.drv.write_register(REG["control"], CONTROL_CODE["Valve manual"])

    def write_ramp(self, ms: int) -> None:
        if 0 < ms < 200:
            raise ValueError("RampMs: 0 (off) or 200..10000 ms")
        self.drv.write_register(REG["ramp"], ms)

    def reset_errors(self) -> None:
        self.drv.write_register(REG["reset_errors"], self.drv.read_register(REG["hw_errors"]))


if __name__ == "__main__":
    run_instrument(Voegtlin)
