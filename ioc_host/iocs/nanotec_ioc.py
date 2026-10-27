"""IOC: Nanotec stepper/BLDC motor controller via Nanotec NanoLib (CiA 402 profile
position mode over Modbus RTU / USB; port of 4dmag fdm/nanotec.py, "rotator").

    python ioc_host/iocs/nanotec_ioc.py --port COM18 --device-id 5 --nanolib-path D:\\git\\4dmag\\old \\
        --prefix FDM:ROTATOR:
    python ioc_host/iocs/nanotec_ioc.py --mock --prefix TEST:ROTATOR:

NanoLib (nanotec_nanolib: Windows .pyd + DLLs, Python <= 3.12) is imported only
when connecting to hardware; --nanolib-path is the folder that contains the
nanotec_nanolib package. Position = raw controller position / --scale
(default 10: the Nanotec default position unit is 0.1 deg, see PositionUnit).

A put of Position runs 4dmag goToPos(): absolute profile-position move, the
drive is switched on for the move and switched off (shutdown) when the target
is reached, unless --hold. MoveRel / JogForward / JogReverse are absolute moves to
the actual position +- a distance. Home starts homing (mode 6, method 0x6098) and
returns; Moving = 1 until the drive reports "homing attained", then the drive is
switched off and back to profile position. Stop (shutdown) or Halt end either.
Startup only reads; the operating mode and the absolute-position bit are set right
before a move.

Objects: CiA 402 / Nanotec object dictionary (e.g. the PD2-C / C5-E online manuals,
nanotec.com "object dictionary"): 6040/6041 control/status word, 6060/6061 mode,
607A target, 6062/6064 demanded/actual position, 606C actual velocity, 6081 profile
velocity, 6083/6084 profile acceleration/deceleration, 6085 quick-stop deceleration,
607E polarity, 607C home offset, 607D software position limits, 6098 homing method,
6099 homing speeds, 609A homing acceleration, 60FD digital inputs (bit 0 negative
limit switch, 1 positive limit switch, 2 home switch), 603F error code, 1001 error
register, 60A8 position unit. Speeds / accelerations are in the controller's units
(Nanotec default rpm, rpm/s). Not exposed: motor current, closed-loop tuning and
I/O configuration (motor-specific commissioning - Plug & Drive Studio).
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

# object dictionary: (index, subindex, bits, signed)
CONTROLWORD, STATUSWORD = (0x6040, 0, 16, False), (0x6041, 0, 16, False)
MODE_SET, MODE_GET = (0x6060, 0, 8, True), (0x6061, 0, 8, True)
TARGET, DEMANDED, ACTUAL = (0x607A, 0, 32, True), (0x6062, 0, 32, True), (0x6064, 0, 32, True)
POLARITY, PROFILE_VELOCITY, POSITION_UNIT = (0x607E, 0, 8, False), (0x6081, 0, 32, False), (0x60A8, 0, 32, False)
VELOCITY_ACTUAL = (0x606C, 0, 32, True)
PROFILE_ACCEL, PROFILE_DECEL, QUICKSTOP_DECEL = (0x6083, 0, 32, False), (0x6084, 0, 32, False), (0x6085, 0, 32, False)
HOME_OFFSET, HOMING_METHOD = (0x607C, 0, 32, True), (0x6098, 0, 8, True)
HOMING_SPEED_SWITCH, HOMING_SPEED_ZERO, HOMING_ACCEL = (0x6099, 1, 32, False), (0x6099, 2, 32, False), (0x609A, 0, 32, False)
SW_LIMIT_MIN, SW_LIMIT_MAX = (0x607D, 1, 32, True), (0x607D, 2, 32, True)
DIGITAL_INPUTS, ERROR_CODE, ERROR_REGISTER = (0x60FD, 0, 32, False), (0x603F, 0, 16, False), (0x1001, 0, 8, False)
MODES = {-2: "Auto setup", -1: "Clock-direction", 0: "No mode", 1: "Profile position", 2: "Velocity",
         3: "Profile velocity", 4: "Profile torque", 6: "Homing", 7: "Interpolated position",
         8: "Cyclic sync position", 9: "Cyclic sync velocity", 10: "Cyclic sync torque"}
PP, HOMING = 1, 6
CW_SWITCH_ON, CW_ENABLE_OP, CW_START, CW_RELATIVE, CW_FAULT_RESET, CW_HALT = 0, 3, 4, 6, 7, 8
SW_OP_ENABLED, SW_FAULT, SW_WARNING, SW_TARGET, SW_LIMIT, SW_ACK, SW_HOMING_ERROR, SW_CLOSED_LOOP = \
    2, 3, 7, 10, 11, 12, 13, 15
DI_NEG_LIMIT, DI_POS_LIMIT, DI_HOME = 0, 1, 2
SI_UNITS = {0x41: "deg", 0x01: "m", 0x10: "rad", 0xB5: "rev", 0x00: "steps"}


class Driver:
    """NanoLib accessor for one device on one bus port (4dmag select_hardware / select_device)."""

    def __init__(self, port: str, device_id: int, nanolib_path: str = "") -> None:
        if nanolib_path:
            sys.path.insert(0, nanolib_path)
            if hasattr(os, "add_dll_directory"):  # the .pyd needs the nanolibm_*.dll next to it
                os.add_dll_directory(str(Path(nanolib_path) / "nanotec_nanolib"))
        from nanotec_nanolib import Nanolib

        self.N = Nanolib
        self.acc = Nanolib.getNanoLibAccessor()
        self.acc.setLoggingLevel(Nanolib.LogLevel_Off)
        hw = [h for h in self._ok(self.acc.listAvailableBusHardware(), "listAvailableBusHardware")
              if re.search(rf"\b{re.escape(port)}\b", h.getName(), re.I)]
        if not hw:
            raise ConnectionError(f"NanoLib: no bus hardware {port}")
        self.hw = hw[-1]  # 4dmag took the last match
        opts = Nanolib.BusHardwareOptions()
        if self.hw.getProtocol() == Nanolib.BUS_HARDWARE_ID_PROTOCOL_MODBUS_RTU:
            opts.addOption(Nanolib.Serial().BAUD_RATE_OPTIONS_NAME, Nanolib.SerialBaudRate().BAUD_RATE_19200)
            opts.addOption(Nanolib.Serial().PARITY_OPTIONS_NAME, Nanolib.SerialParity().EVEN)
        elif self.hw.getProtocol() == Nanolib.BUS_HARDWARE_ID_PROTOCOL_CANOPEN:
            opts.addOption(Nanolib.CanBus().BAUD_RATE_OPTIONS_NAME, Nanolib.CanBaudRate().BAUD_RATE_1000K)
        self._ok(self.acc.openBusHardwareWithProtocol(self.hw, opts), "openBusHardware")
        try:
            callback = type("Quiet", (Nanolib.NlcScanBusCallback,),
                            {"callback": lambda self, info, found, data: Nanolib.ResultVoid()})()
            devices = self._ok(self.acc.scanDevices(self.hw, callback), "scanDevices")
            match = [d for d in devices if re.search(rf"device id: {device_id}\b", d.toString())]
            if not match:
                raise ConnectionError(f"NanoLib: device id {device_id} not found on {port}: "
                                      f"{[d.toString() for d in devices]}")
            self.dev = self._ok(self.acc.addDevice(match[0]), "addDevice")
            self._ok(self.acc.connectDevice(self.dev), "connectDevice")
        except Exception:
            self.acc.closeBusHardware(self.hw)
            raise

    @staticmethod
    def _ok(result, what):
        if result.hasError():
            raise ConnectionError(f"NanoLib {what}: {result.getError()}")
        return result.getResult() if hasattr(result, "getResult") else None

    def read(self, obj) -> int:
        index, sub, bits, signed = obj
        r = self.acc.readNumber(self.dev, self.N.OdIndex(index, sub))
        if r.hasError():
            raise OSError(f"NanoLib read {index:04X}:{sub:02X}: {r.getError()}")
        v = int(r.getResult()) & ((1 << bits) - 1)
        return v - (1 << bits) if signed and v >= 1 << (bits - 1) else v

    def write(self, obj, value: int) -> None:
        index, sub, bits, _ = obj
        r = self.acc.writeNumber(self.dev, int(value) & ((1 << bits) - 1), self.N.OdIndex(index, sub), bits)
        if r.hasError():
            raise OSError(f"NanoLib write {index:04X}:{sub:02X}={value}: {r.getError()}")

    def close(self):
        try:
            self.acc.disconnectDevice(self.dev)
        finally:
            self.acc.closeBusHardware(self.hw)


class SimDriver:
    """CiA 402 drive in profile-position mode: state machine, set-point handshake, homing, halt."""

    def __init__(self) -> None:
        self.od = {CONTROLWORD[:2]: 0, MODE_SET[:2]: 0, TARGET[:2]: 0, POLARITY[:2]: 0, PROFILE_VELOCITY[:2]: 3600,
                   POSITION_UNIT[:2]: 0xFF410000, PROFILE_ACCEL[:2]: 5000, PROFILE_DECEL[:2]: 5000,
                   QUICKSTOP_DECEL[:2]: 10000, HOME_OFFSET[:2]: 0, HOMING_METHOD[:2]: 19,
                   HOMING_SPEED_SWITCH[:2]: 1800, HOMING_SPEED_ZERO[:2]: 300, HOMING_ACCEL[:2]: 5000,
                   SW_LIMIT_MIN[:2]: -2 ** 31, SW_LIMIT_MAX[:2]: 2 ** 31 - 1, ERROR_CODE[:2]: 0, ERROR_REGISTER[:2]: 0}
        self.pos, self.goal, self.homing, self.velocity, self.t = 450.0, None, False, 0.0, time.monotonic()

    def _enabled(self):
        return self.od[CONTROLWORD[:2]] & 0b1111 == 0b1111

    def _update(self):
        now = time.monotonic()
        dt, self.t = now - self.t, now
        self.velocity = 0.0
        if self.goal is not None and self._enabled() and not self.od[CONTROLWORD[:2]] >> CW_HALT & 1:
            v = self.od[(HOMING_SPEED_SWITCH if self.homing else PROFILE_VELOCITY)[:2]] * 2
            d = self.goal - self.pos
            self.pos = self.goal if abs(d) <= v * dt else self.pos + v * dt * (1 if d > 0 else -1)
            self.velocity = v if self.pos != self.goal else 0.0

    def read(self, obj) -> int:
        self._update()
        key = obj[:2]
        if key == STATUSWORD[:2]:
            cw = self.od[CONTROLWORD[:2]]
            sw = 0b0001 | (cw & 1) << 1 | (self._enabled() << 2) | 1 << 5 | 1 << 4 | 1 << SW_CLOSED_LOOP
            reached = self.goal is None or self.pos == self.goal or bool(cw >> CW_HALT & 1)
            sw |= reached << SW_TARGET | (self.homing and reached) << SW_ACK
            return sw
        if key == MODE_GET[:2]:
            return self.od[MODE_SET[:2]]
        if key == ACTUAL[:2]:
            return int(round(self.pos))
        if key == DEMANDED[:2]:
            return int(self.goal if self.goal is not None else self.pos)
        if key == VELOCITY_ACTUAL[:2]:
            return int(self.velocity)
        if key == DIGITAL_INPUTS[:2]:
            return int(abs(self.pos) < 1) << DI_HOME  # the simulated home switch is at 0
        return self.od[key]

    def write(self, obj, value: int) -> None:
        self._update()
        key = obj[:2]
        if key == CONTROLWORD[:2]:
            rising = value >> CW_START & 1 and not self.od[key] >> CW_START & 1
            if value >> CW_FAULT_RESET & 1:
                self.od[ERROR_CODE[:2]] = 0
            self.od[key] = value
            if rising and self._enabled():
                if self.od[MODE_SET[:2]] == HOMING:
                    self.goal, self.homing = float(self.od[HOME_OFFSET[:2]]), True
                else:
                    target = self.od[TARGET[:2]] + (round(self.pos) if value >> CW_RELATIVE & 1 else 0)
                    lo, hi = self.od[SW_LIMIT_MIN[:2]], self.od[SW_LIMIT_MAX[:2]]
                    self.goal, self.homing = float(min(max(target, lo), hi)), False
            return
        self.od[key] = value


def _scaled(obj):
    return lambda s: s.drv.read(obj) / s.args.scale


def _set_scaled(obj):
    return lambda s, v: s.drv.write(obj, int(round(v * s.args.scale)))


def _raw(obj, **kw) -> Param:
    """A setting in the controller's own units, read once (and after puts)."""
    return Param(int, poll=0, kind="config", get=lambda s: s.drv.read(obj), set=lambda s, v: s.drv.write(obj, v), **kw)


class Nanotec(Instrument):
    TYPE = "nanotec"
    DESCRIPTION = "Nanotec motor controller (NanoLib, profile-position mode): sample rotator"
    DEFAULT_PREFIX = "FDM:ROTATOR:"
    POLL = 1.0
    ophyd = {"stop": "Stop"}

    Position = Param(float, units="deg", precision=3, get=_scaled(ACTUAL), set="move",
                     poll=0.2, kind="hinted", overview=True, trend=True,
                     positioner={"readback": "Position", "moving": "Moving", "tolerance": 0.15, "settle_s": 0.0,
                                 "timeout_s": 600.0},
                     doc="absolute position (actual position 0x6064 / scale)")
    Moving = Param(bool, get="moving", poll=0.2, kind="omitted", overview=True, doc="moving or homing")
    Target = Param(float, units="deg", precision=3, get=_scaled(DEMANDED), kind="config",
                   doc="demanded position 0x6062")
    VelocityActual = Param(int, get=lambda s: s.drv.read(VELOCITY_ACTUAL), poll=0.5, kind="config",
                           doc="actual velocity 0x606C (controller units, default rpm)")
    Velocity = _raw(PROFILE_VELOCITY, limits=(1, 100000), safety="safe",
                    doc="profile velocity 0x6081 in the controller's velocity unit (default rpm)")
    Acceleration = _raw(PROFILE_ACCEL, limits=(1, 1_000_000), safety="safe",
                        doc="profile acceleration 0x6083 (controller units, default rpm/s)")
    Deceleration = _raw(PROFILE_DECEL, limits=(1, 1_000_000), safety="safe",
                        doc="profile deceleration 0x6084 (controller units, default rpm/s)")
    QuickStopDecel = _raw(QUICKSTOP_DECEL, limits=(1, 10_000_000),
                          doc="quick-stop deceleration 0x6085 (controller units)")
    SoftLimitMin = Param(float, units="deg", precision=3, poll=0, kind="config", get=_scaled(SW_LIMIT_MIN),
                         set=_set_scaled(SW_LIMIT_MIN), doc="software position limit 0x607D:1 (targets are clipped)")
    SoftLimitMax = Param(float, units="deg", precision=3, poll=0, kind="config", get=_scaled(SW_LIMIT_MAX),
                         set=_set_scaled(SW_LIMIT_MAX), doc="software position limit 0x607D:2")
    # homing method / offset decide where the rotator drives and what 0 means -> protected
    HomingMethod = _raw(HOMING_METHOD, limits=(-128, 127), safety="protected",
                        doc="homing method 0x6098 (CiA 402 / Nanotec manual; 35 or 37 = current position is 0)")
    HomeOffset = Param(float, units="deg", precision=3, poll=0, kind="config", get=_scaled(HOME_OFFSET),
                       set=_set_scaled(HOME_OFFSET), safety="protected", doc="home offset 0x607C")
    HomingSpeed = _raw(HOMING_SPEED_SWITCH, limits=(1, 100000), safety="safe",
                       doc="homing speed while searching the switch 0x6099:1 (controller units)")
    HomingSpeedZero = _raw(HOMING_SPEED_ZERO, limits=(1, 100000), safety="safe",
                           doc="homing speed while searching zero 0x6099:2 (controller units)")
    HomingAccel = _raw(HOMING_ACCEL, limits=(1, 1_000_000), safety="safe", doc="homing acceleration 0x609A")
    Mode = Param(str, get=lambda s: MODES.get(s.drv.read(MODE_GET), "?"), poll=5.0, kind="config")
    StatusWord = Param(int, get="status_word", kind="omitted", doc="CiA 402 statusword 0x6041")
    Enabled = Param(bool, get=lambda s: s.status_word() >> SW_OP_ENABLED & 1, kind="config",
                    doc="operation enabled (motor powered)")
    Fault = Param(bool, get=lambda s: s.status_word() >> SW_FAULT & 1, overview=True)
    Warning = Param(bool, get=lambda s: s.status_word() >> SW_WARNING & 1, kind="config", doc="statusword warning bit")
    LimitActive = Param(bool, get=lambda s: s.status_word() >> SW_LIMIT & 1, kind="config",
                        doc="internal limit active (software / hardware limit reached)")
    ClosedLoop = Param(bool, get=lambda s: s.status_word() >> SW_CLOSED_LOOP & 1, kind="config",
                       doc="closed-loop (encoder) operation active (Nanotec statusword bit 15)")
    ErrorCode = Param(str, get=lambda s: f"{s.drv.read(ERROR_CODE):#06x}", kind="config",
                      doc="last error code 0x603F (0x0000 = none; meaning in the controller manual)")
    ErrorRegister = Param(int, get=lambda s: s.drv.read(ERROR_REGISTER), kind="omitted", doc="error register 0x1001")
    NegLimitSwitch = Param(bool, get=lambda s: s.inputs() >> DI_NEG_LIMIT & 1, kind="config",
                           doc="negative limit switch input (0x60FD bit 0)")
    PosLimitSwitch = Param(bool, get=lambda s: s.inputs() >> DI_POS_LIMIT & 1, kind="config",
                           doc="positive limit switch input (0x60FD bit 1)")
    HomeSwitch = Param(bool, get=lambda s: s.inputs() >> DI_HOME & 1, kind="config",
                       doc="home switch input (0x60FD bit 2)")
    Reversed = Param(bool, get=lambda s: s.drv.read(POLARITY) >> 7 & 1, poll=0, kind="config",
                     set=lambda s, v: s.drv.write(POLARITY, 0xC0 if v else 0),
                     doc="position + velocity polarity 0x607E bits 7/6 (4dmag setPolarity: 192 / 0)")
    PositionUnit = Param(str, get="unit_text", poll=0, kind="config", doc="0x60A8 (SI unit of the raw position)")
    JogStep = Param(float, units="deg", precision=3, writable=True, initial=1.0, limits=(0.0, 1e6), kind="config",
                    safety="safe", doc="step of JogForward / JogReverse")

    MoveRel = Action(lambda s, v: s.move(s.drv.read(ACTUAL) / s.args.scale + v), arg=float,
                     doc="move by this many user units from the actual position")
    JogForward = Action(lambda s: s.move(s.drv.read(ACTUAL) / s.args.scale + s.values["JogStep"]),
                        doc="move +JogStep")
    JogReverse = Action(lambda s: s.move(s.drv.read(ACTUAL) / s.args.scale - s.values["JogStep"]),
                        doc="move -JogStep")
    Stop = Action("stop", safety="safe", doc="shutdown (controlword 6): the drive stops and is switched off",
                  refresh=["Position", "Moving", "Enabled"])
    Halt = Action("halt", safety="safe", doc="halt (controlword bit 8): decelerate with the profile deceleration, "
                                             "stay enabled", refresh=["Position", "Moving"])
    # a rotator with cables / a sample holder can wind up while searching the switch -> protected
    Home = Action("home", safety="protected", refresh=["Moving", "Mode"],
                  doc="start homing (mode 6) with HomingMethod; Moving = 1 until homing attained")
    FaultReset = Action("fault_reset", doc="controlword bit 7 rising edge", refresh=["Fault", "StatusWord", "ErrorCode"])

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--port", default="COM18", help="bus hardware name as NanoLib lists it (COM port)")
        parser.add_argument("--device-id", type=int, default=5, help="Modbus id of the controller")
        parser.add_argument("--nanolib-path", default="", help="folder that contains the nanotec_nanolib package")
        parser.add_argument("--scale", type=float, default=10.0, help="raw position units per user unit")
        parser.add_argument("--unit", default="deg")
        parser.add_argument("--tolerance", type=float, default=0.15, help="positioner tolerance (user units)")
        parser.add_argument("--hold", action="store_true", help="keep the drive enabled after a move (4dmag: off)")
        parser.add_argument("--move-timeout", type=float, default=600.0, help="s; a move/homing not done by then fails")

    def __init__(self, args) -> None:
        super().__init__(args)
        self.pending, self.homing, self.t_move = False, False, 0.0
        self._sw, self._sw_t, self._di, self._di_t = 0, 0.0, 0, 0.0

    def instance_schema(self) -> dict:
        schema = self.schema()
        for p in schema["params"]:
            if p.get("units") == "deg":
                p["units"] = self.args.unit
            if p.get("positioner"):
                p["positioner"] = {**p["positioner"], "tolerance": self.args.tolerance,
                                   "timeout_s": self.args.move_timeout}
        return schema

    def open(self) -> None:
        a = self.args
        self.drv = SimDriver() if self.mock else Driver(a.port, a.device_id, a.nanolib_path)
        self.pending = self.homing = False

    def status_word(self) -> int:
        """0x6041 once per poll round (its bits are separate params)."""
        if time.monotonic() - self._sw_t > 0.1:
            self._sw, self._sw_t = self.drv.read(STATUSWORD), time.monotonic()
        return self._sw

    def inputs(self) -> int:
        if time.monotonic() - self._di_t > 0.1:
            self._di, self._di_t = self.drv.read(DIGITAL_INPUTS), time.monotonic()
        return self._di

    # controlword read-modify-write, as 4dmag SwitchedOn()/EnableOperation()/StartTravel()
    def cw_bit(self, bit: int, on: bool) -> None:
        cw = self.drv.read(CONTROLWORD)
        self.drv.write(CONTROLWORD, cw | 1 << bit if on else cw & ~(1 << bit))

    def set_mode(self, mode: int) -> None:
        if self.drv.read(MODE_GET) != mode:
            self.drv.write(CONTROLWORD, 6)  # 4dmag stop_motor() before a mode change
            self.drv.write(MODE_SET, mode)
            if self.drv.read(MODE_GET) != mode:
                raise RuntimeError(f"Nanotec: mode {MODES.get(mode)} not accepted")

    def move(self, value: float) -> None:
        if self.drv.read(STATUSWORD) >> SW_FAULT & 1:
            raise RuntimeError("Nanotec: drive in fault - FaultReset first")
        if self.homing:
            raise RuntimeError("Nanotec: homing in progress - Stop it first")
        self.set_mode(PP)
        cw = self.drv.read(CONTROLWORD) & ~(1 << CW_RELATIVE | 1 << CW_HALT | 1 << CW_START)  # absolute, no halt
        self.drv.write(CONTROLWORD, cw & ~(1 << CW_SWITCH_ON | 1 << CW_ENABLE_OP))
        self.drv.write(TARGET, int(round(value * self.args.scale)))
        self.cw_bit(CW_SWITCH_ON, True)
        self.cw_bit(CW_ENABLE_OP, True)
        self.cw_bit(CW_START, True)  # rising edge = new set-point
        self.pending, self.t_move, self._sw_t = True, time.monotonic(), 0.0

    def moving(self) -> bool:
        if not self.pending:
            return False
        sw = self.drv.read(STATUSWORD)
        late = time.monotonic() - self.t_move > self.args.move_timeout
        if self.homing:
            error = sw >> SW_HOMING_ERROR & 1 or sw >> SW_FAULT & 1
            if error or late or (sw >> SW_ACK & 1 and sw >> SW_TARGET & 1):
                self.pending = self.homing = False
                self.drv.write(CONTROLWORD, 6)
                self.set_mode(PP)
                if error or late:
                    raise RuntimeError(f"Nanotec: homing {'error' if error else 'timeout'} (statusword {sw:#06x})")
            return self.pending
        done = time.monotonic() - self.t_move > 0.2 and sw >> SW_TARGET & 1
        if done or sw >> SW_FAULT & 1 or late:
            self.pending = False
            if not self.args.hold:
                self.cw_bit(CW_SWITCH_ON, False)  # 4dmag: switched off after reaching the target
        return self.pending

    def stop(self) -> None:
        self.drv.write(CONTROLWORD, 6)
        self.pending = self.homing = False
        self._sw_t = 0.0

    def halt(self) -> None:
        self.cw_bit(CW_HALT, True)
        self._sw_t = 0.0

    def home(self) -> None:
        if self.drv.read(STATUSWORD) >> SW_FAULT & 1:
            raise RuntimeError("Nanotec: drive in fault - FaultReset first")
        self.set_mode(HOMING)
        cw = self.drv.read(CONTROLWORD) & ~(1 << CW_START | 1 << CW_HALT)
        for c in (cw & ~0b1111 | 0b0110, cw & ~0b1111 | 0b0111, cw & ~0b1111 | 0b1111):  # shutdown, on, enable
            self.drv.write(CONTROLWORD, c)
        self.cw_bit(CW_START, True)
        self.pending, self.homing, self.t_move, self._sw_t = True, True, time.monotonic(), 0.0

    def fault_reset(self) -> None:
        self.cw_bit(CW_FAULT_RESET, False)
        self.cw_bit(CW_FAULT_RESET, True)
        self.cw_bit(CW_FAULT_RESET, False)
        self._sw_t = 0.0

    def unit_text(self) -> str:
        v = self.drv.read(POSITION_UNIT)
        exp = (v >> 24) - 256 if v >> 24 > 127 else v >> 24
        return f"{v:#010x} = 10^{exp} {SI_UNITS.get(v >> 16 & 0xFF, '?')}"

    def on_exit(self) -> None:
        if self.pending and self.drv is not None:  # do not leave a move / homing running unattended
            self.stop()


if __name__ == "__main__":
    run_instrument(Nanotec)
