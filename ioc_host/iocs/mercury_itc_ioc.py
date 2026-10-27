"""IOC: Oxford Instruments Mercury iTC temperature controller (VISA: GPIB / USB / Ethernet).

    python ioc_host/iocs/mercury_itc_ioc.py --resource GPIB0::24::INSTR --prefix FASTQ:MERCURY1:
    python ioc_host/iocs/mercury_itc_ioc.py --protocol scpi --resource TCPIP0::192.168.1.30::7020::SOCKET \
        --sensor MB1.T1 --prefix FASTQ:MERCURY1:
    python ioc_host/iocs/mercury_itc_ioc.py --mock --tolerance 0.2 --settle-s 2 --prefix TEST:MERCURY1:

--protocol legacy (default) speaks the ITC503 command set the Mercury emulates -
what TransportMeasure (itc503s_and_lakeshore.py) uses in the lab: R<n> reads,
'$'-prefixed writes (no reply), sweep table for ramps. --protocol scpi speaks the
Mercury's native READ:/SET:DEV:<uid>:TEMP:... language (not used in the lab yet);
heater volts/power/limit, needle valve and pressure need the board UIDs (--heater,
--aux, --pressure: see the front panel / READ:SYS:CAT). Paths: Mercury iTC manual and
the PyMeasure / frappy (PSI) drivers. Params a protocol cannot read keep their last value.

Safety: startup only reads; modes are changed only by an explicit put
(HeaterGasMode, or a Setpoint/RampStart put while AutoHeater = 1, which switches
the heater - never the gas - to auto, as itc503_ioc.py does).
"""
from __future__ import annotations

import math
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

MODES = ("MANUAL", "AM", "MA", "AUTO")  # heater/gas manual-auto, ITC503 $A0..3
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


class Legacy:
    """ITC503 command set. Replies are 'R<value>'; the Mercury sends the heater
    (R5) without '+', and a temperature reply WITH '+' is a stale reply to an
    earlier query (TransportMeasure's desync check) - asked again."""

    def __init__(self, io) -> None:
        self.io = io
        self._x, self._x_t = "", 0.0

    def read(self, n: int) -> float:
        reply = ""
        for _ in range(10):
            reply = self.io.query(f"R{n}").strip()
            if reply[:1] == "R" and not (n < 5 and reply[1:2] == "+"):
                m = _NUM.match(reply[1:])
                if m:
                    return float(m.group(0))
            time.sleep(0.1)
        raise RuntimeError(f"no valid reply to R{n} (last {reply!r})")

    def cmd(self, *commands: str) -> None:
        self.io.write("$C3")  # remote & unlocked, as TransportMeasure does on connect
        for c in commands:
            self.io.write("$" + c)

    def status(self) -> dict:
        """X -> 'X0A1C3S00H1L0' (system, auto/man, loc/rem, sweep, heater sensor, auto-PID), cached 0.5 s."""
        if time.monotonic() - self._x_t > 0.5:
            self._x, self._x_t = self.io.query("X").strip(), time.monotonic()
        m = re.match(r"X(\d)A(\d)C(\d)S(\d+)H(\d)L(\d)", self._x)
        if not m:
            raise RuntimeError(f"unexpected status reply {self._x!r}")
        return dict(zip(("X", "A", "C", "S", "H", "L"), map(int, m.groups())))

    temp = lambda self, i: self.read(i)  # noqa: E731
    setpoint = lambda self: self.read(0)  # noqa: E731
    temp_error = lambda self: self.read(4)  # noqa: E731
    heater = lambda self: self.read(5)  # noqa: E731
    heater_volts = lambda self: self.read(6)  # noqa: E731
    gas = lambda self: self.read(7)  # noqa: E731
    pid = lambda self, k: self.read({"P": 8, "I": 9, "D": 10}[k])  # noqa: E731
    mode = lambda self: MODES[self.status()["A"]]  # noqa: E731
    ramping = lambda self: self.status()["S"] != 0  # noqa: E731
    auto_pid = lambda self: self.status()["L"]  # noqa: E731
    heater_sensor = lambda self: self.status()["H"]  # noqa: E731
    # no legacy equivalent: published as "no value" (the PV keeps its last value)
    heater_power = pressure = heater_max = alarms = lambda self: None  # noqa: E731

    def set_setpoint(self, v):
        self.cmd(f"T{v:.4f}")

    def set_mode(self, mode):
        self.cmd(f"A{MODES.index(mode)}")

    def set_pid(self, k, v):
        self.cmd(f"{k}{v:g}")

    def set_heater_manual(self, pct):
        self.cmd(f"O{pct:.1f}")

    def set_gas_manual(self, pct):
        self.cmd(f"G{pct:.1f}")

    def set_auto_pid(self, on):
        self.cmd(f"L{int(on)}")

    def set_heater_sensor(self, n):
        self.cmd(f"H{n}")

    def set_heater_max(self, v):
        self.cmd(f"M{v:g}")

    def ramp_start(self, target, minutes, hold_min=0.0):
        """Sweep table step 1 = (target, sweep time, hold time); steps 2-16 hold the
        target with zero times (PyMeasure's padding), then S1 = start
        (TransportMeasure Scripts/itc503s_sweep.py)."""
        self.cmd("S0")
        for step in range(1, 17):
            t, h = (minutes, hold_min) if step == 1 else (0.0, 0.0)
            self.cmd(f"x{step}", "y1", f"s{target:.4f}", "y2", f"s{t:.2f}", "y3", f"s{h:.2f}")
        self.cmd("x0", "y0", "S1")
        self._x_t = 0.0

    def ramp_stop(self):
        self.cmd("S0")  # the ITC holds the setpoint it had reached
        self._x_t = 0.0


class Scpi:
    """Mercury native protocol: 'READ:DEV:MB1.T1:TEMP:SIG:TEMP' ->
    'STAT:DEV:MB1.T1:TEMP:SIG:TEMP:4.2000K'; SET replies end ':VALID'."""

    def __init__(self, io, sensors: list[str], heater: str = "", aux: str = "", pressure: str = "") -> None:
        self.io, self.sensors = io, sensors
        self.loop = f"DEV:{sensors[0]}:TEMP:LOOP"
        self.htr, self.aux, self.pres = heater, aux, pressure  # board UIDs, "" = not fitted / not given

    def _read(self, path: str) -> str:
        reply = self.io.query(f"READ:{path}").strip()
        if "INVALID" in reply or not reply.startswith("STAT:"):
            raise RuntimeError(f"READ:{path} -> {reply!r}")
        return reply.rsplit(":", 1)[-1]

    def _num(self, path: str) -> float:
        m = _NUM.match(self._read(path))
        if not m:
            raise RuntimeError(f"READ:{path}: no number")
        return float(m.group(0))

    def _set(self, path: str, value) -> None:
        reply = self.io.query(f"SET:{path}:{value}").strip()
        if not reply.endswith(":VALID"):
            raise RuntimeError(f"SET:{path}:{value} -> {reply!r}")

    def temp(self, i):
        return self._num(f"DEV:{self.sensors[i - 1]}:TEMP:SIG:TEMP") if i <= len(self.sensors) else None

    setpoint = lambda self: self._num(f"{self.loop}:TSET")  # noqa: E731
    heater = lambda self: self._num(f"{self.loop}:HSET")  # noqa: E731
    pid = lambda self, k: self._num(f"{self.loop}:{k}")  # noqa: E731
    ramping = lambda self: self._read(f"{self.loop}:RENA") == "ON"  # noqa: E731
    auto_pid = lambda self: self._read(f"{self.loop}:PIDT") == "ON"  # noqa: E731
    heater_sensor = lambda self: None  # noqa: E731

    def temp_error(self):
        return self.setpoint() - self.temp(1)

    def gas(self):
        """needle valve position from the auxiliary (gas) board if given, else the loop's manual flow."""
        return self._num(f"DEV:{self.aux}:AUX:SIG:PERC") if self.aux else self._num(f"{self.loop}:FSET")

    def heater_volts(self):
        return self._num(f"DEV:{self.htr}:HTR:SIG:VOLT") if self.htr else None

    def heater_power(self):
        return self._num(f"DEV:{self.htr}:HTR:SIG:POWR") if self.htr else None

    def heater_max(self):
        return self._num(f"DEV:{self.htr}:HTR:VLIM") if self.htr else None

    def pressure(self):
        return self._num(f"DEV:{self.pres}:PRES:SIG:PRES") if self.pres else None

    def alarms(self):
        """READ:SYS:ALRM -> 'STAT:SYS:ALRM:<board>\\t<message>;...' (empty = no alarm)."""
        reply = self.io.query("READ:SYS:ALRM").strip()
        if not reply.startswith("STAT:SYS:ALRM"):
            raise RuntimeError(f"READ:SYS:ALRM -> {reply!r}")
        return reply[len("STAT:SYS:ALRM"):].lstrip(":").replace("\t", " ") or "none"

    def set_auto_pid(self, on):
        self._set(f"{self.loop}:PIDT", "ON" if on else "OFF")

    def set_heater_max(self, v):
        if not self.htr:
            raise ValueError("needs --heater <heater board UID>")
        self._set(f"DEV:{self.htr}:HTR:VLIM", f"{v:g}")

    def mode(self):
        h, g = self._read(f"{self.loop}:ENAB") == "ON", self._read(f"{self.loop}:FAUT") == "ON"
        return MODES[h + 2 * g]

    def set_setpoint(self, v):
        self._set(f"{self.loop}:TSET", f"{v:.4f}")

    def set_mode(self, mode):
        i = MODES.index(mode)
        self._set(f"{self.loop}:ENAB", "ON" if i & 1 else "OFF")
        self._set(f"{self.loop}:FAUT", "ON" if i & 2 else "OFF")

    def set_pid(self, k, v):
        self._set(f"{self.loop}:{k}", f"{v:g}")

    def set_heater_manual(self, pct):
        self._set(f"{self.loop}:HSET", f"{pct:.1f}")

    def set_gas_manual(self, pct):
        self._set(f"{self.loop}:FSET", f"{pct:.1f}")

    def _legacy_only(self, *_):
        raise NotImplementedError("only with --protocol legacy")

    set_heater_sensor = _legacy_only

    def ramp_start(self, target, minutes, hold_min=0.0):
        """Rate from the present temperature, ramp enabled, then the target."""
        rate = abs(target - self.temp(1)) / max(minutes, 1e-3)
        self._set(f"{self.loop}:RSET", f"{max(rate, 0.001):.4f}")
        self._set(f"{self.loop}:RENA", "ON")
        self.set_setpoint(target)

    def ramp_stop(self):
        self._set(f"{self.loop}:RENA", "OFF")
        self.set_setpoint(self.temp(1))  # hold where we are, not jump to the target


class SimMercuryIO:
    """Answers both protocols. First-order thermal model (tau_s real seconds) towards
    the setpoint while the heater is auto; ramps run time_scale x faster."""

    def __init__(self, start_k: float = 10.0, tau_s: float = 3.0, time_scale: float = 60.0) -> None:
        self.t, self.sp, self.tau, self.scale = start_k, start_k, tau_s, time_scale
        self.mode, self.pid, self.gas_pct, self.htr_pct = 1, {"P": 25.0, "I": 1.0, "D": 0.0}, 20.0, 0.0
        self.ramp, self.rena, self.rset, self.table = None, False, 1.0, {}
        self.ptr, self.hsens, self.lpid, self._last = [0, 0], 1, 0, time.monotonic()
        self.vlim = 40.0

    def _step(self):
        now = time.monotonic()
        dt, self._last = now - self._last, now
        if self.ramp:
            a, b, t0, dur = self.ramp
            frac = min((now - t0) / dur, 1.0) if dur > 0 else 1.0
            self.sp = a + (b - a) * frac
            if frac >= 1.0:
                self.ramp = None
        if self.mode & 1:
            self.t += (self.sp - self.t) * (1 - math.exp(-dt / self.tau))
            self.htr_pct = max(0.0, min(100.0, 10.0 + 5.0 * (self.sp - self.t)))
        else:  # manual heater: equilibrium above a 4.2 K bath grows with the output
            self.t += (4.2 + 3.0 * self.htr_pct - self.t) * (1 - math.exp(-dt / self.tau))
        return self.t + random.gauss(0, 1e-4)

    def write(self, cmd: str) -> None:
        c = cmd.strip().lstrip("$")
        k, v = c[:1], c[1:]
        if k == "T":
            self.sp, self.ramp = float(v), None
        elif k == "A":
            self.mode = int(v)
        elif k in "PID" and v:
            self.pid[k] = float(v)
        elif k == "O":
            self.htr_pct = float(v)
        elif k == "G":
            self.gas_pct = float(v)
        elif k == "H":
            self.hsens = int(v)
        elif k == "L":
            self.lpid = int(v)
        elif k == "M":
            self.vlim = float(v)
        elif k == "x":
            self.ptr[0] = int(v)
        elif k == "y":
            self.ptr[1] = int(v)
        elif k == "s":
            self.table[tuple(self.ptr)] = float(v)
        elif k == "S":
            self._step()
            if int(v) and (1, 1) in self.table:
                dur = self.table.get((1, 2), 0.0) * 60.0 / self.scale
                self.ramp = (self.sp, self.table[(1, 1)], time.monotonic(), dur)
            else:
                self.ramp = None

    def query(self, cmd: str) -> str:
        c = cmd.strip()
        t = self._step()
        if c == "X":
            return f"X0A{self.mode}C3S{1 if self.ramp else 0:02d}H{self.hsens}L{self.lpid}"
        if c[:1] == "R" and c[1:].isdigit():
            n = int(c[1:])
            v = {0: self.sp, 1: t, 2: t + 0.8, 3: t + 0.9, 4: self.sp - t, 5: self.htr_pct,
                 6: self.htr_pct / 100 * self.vlim, 7: self.gas_pct, 8: self.pid["P"], 9: self.pid["I"],
                 10: self.pid["D"]}[n]
            return f"R{v:.3f}" if n != 5 else f"R{v:.1f}"
        if c == "READ:SYS:ALRM":
            return "STAT:SYS:ALRM:"
        verb, _, path = c.partition(":")
        parts = path.split(":")
        if verb == "READ":
            key = parts[-1]
            volts = self.htr_pct / 100 * self.vlim
            v = {"TEMP": f"{t + (0.8 if parts[1] != 'MB1.T1' else 0):.4f}K", "TSET": f"{self.sp:.4f}K",
                 "HSET": f"{self.htr_pct:.2f}", "FSET": f"{self.gas_pct:.2f}", "ENAB": "ON" if self.mode & 1 else "OFF",
                 "FAUT": "ON" if self.mode & 2 else "OFF", "RENA": "ON" if self.rena else "OFF",
                 "RSET": f"{self.rset:.4f}K/m", "PIDT": "ON" if self.lpid else "OFF", "VOLT": f"{volts:.3f}V",
                 "POWR": f"{volts ** 2 / 50.0:.4f}W", "VLIM": f"{self.vlim:.2f}", "PERC": f"{self.gas_pct:.2f}%",
                 "PRES": f"{5.0 + 0.1 * self.gas_pct:.3f}mB"}.get(key, self.pid.get(key))
            return f"STAT:{path}:{v}" if v is not None else f"STAT:{path}:INVALID"
        if verb == "SET":
            key, val = parts[-2], parts[-1]
            if key == "PIDT":
                self.lpid = int(val == "ON")
            elif key == "VLIM":
                self.vlim = float(val)
            elif key == "TSET":
                self.sp = float(val)
                if self.rena:
                    self.ramp = (t, self.sp, time.monotonic(), abs(self.sp - t) / self.rset * 60.0 / self.scale)
            elif key in ("ENAB", "FAUT"):
                bit = 1 if key == "ENAB" else 2
                self.mode = (self.mode | bit) if val == "ON" else (self.mode & ~bit)
            elif key == "RENA":
                self.rena = val == "ON"
                self.ramp = None if not self.rena else self.ramp
            elif key == "RSET":
                self.rset = float(val)
            elif key == "HSET":
                self.htr_pct = float(val)
            elif key == "FSET":
                self.gas_pct = float(val)
            elif key in self.pid:
                self.pid[key] = float(val)
            return f"STAT:{path}:VALID"
        return "?"


class MercuryITC(Instrument):
    TYPE = "mercury_itc"
    DESCRIPTION = "Oxford Instruments Mercury iTC temperature controller (ITC503 legacy or native SCPI protocol)"
    DEFAULT_PREFIX = "FASTQ:MERCURY1:"
    POLL = 1.0

    Temp = Param(float, units="K", precision=4, get=lambda s: s.drv.temp(1), kind="hinted", overview=True,
                 trend=True, doc="control sensor temperature (legacy R1 / first --sensor)")
    Temp2 = Param(float, units="K", precision=4, get=lambda s: s.drv.temp(2), poll=2.0, doc="sensor 2")
    Temp3 = Param(float, units="K", precision=4, get=lambda s: s.drv.temp(3), poll=2.0, doc="sensor 3")
    TempError = Param(float, units="K", precision=4, get=lambda s: s.drv.temp_error(), poll=2.0,
                      doc="setpoint - control temperature (legacy R4)")
    Setpoint = Param(float, units="K", precision=4, limits=(0.0, 1000.0), get=lambda s: s.drv.setpoint(),
                     set="write_setpoint", poll=2.0, overview=True, refresh=["HeaterGasMode"],
                     positioner={"readback": "Temp", "tolerance": 0.2, "settle_s": 60.0, "timeout_s": 7200.0},
                     doc="temperature setpoint; set() completes when |Temp - setpoint| <= tolerance for settle_s")
    Heater = Param(float, units="%", precision=1, get=lambda s: s.drv.heater(), overview=True,
                   doc="heater output (% of max)")
    HeaterVolts = Param(float, units="V", precision=2, get=lambda s: s.drv.heater_volts(), poll=2.0,
                        doc="heater voltage (legacy R6; scpi needs --heater)")
    HeaterPower = Param(float, units="W", precision=4, get=lambda s: s.drv.heater_power(), poll=2.0,
                        doc="heater power (scpi with --heater only)")
    GasFlow = Param(float, units="%", precision=1, get=lambda s: s.drv.gas(), poll=2.0,
                    doc="needle valve (legacy R7; scpi: --aux board, else the loop's manual flow)")
    Pressure = Param(float, units="mbar", precision=3, get=lambda s: s.drv.pressure(), poll=2.0,
                     doc="pressure board reading (scpi with --pressure only)")
    Alarms = Param(str, event="change", get=lambda s: s.drv.alarms(), poll=5.0,
                   doc="active alarms (scpi READ:SYS:ALRM)")
    HeaterGasMode = Param("enum", choices=MODES, get=lambda s: s.drv.mode(), set=lambda s, v: s.drv.set_mode(v),
                          poll=2.0, kind="config", doc="heater/gas manual-auto (AM = heater auto, gas manual)")
    Ramping = Param(bool, get=lambda s: s.drv.ramping(), poll=2.0, doc="a ramp (sweep) is running")
    P = Param(float, precision=2, get=lambda s: s.drv.pid("P"), set=lambda s, v: s.drv.set_pid("P", v), poll=10.0,
              kind="config", doc="proportional band")
    I = Param(float, units="min", precision=2, get=lambda s: s.drv.pid("I"), set=lambda s, v: s.drv.set_pid("I", v),
              poll=10.0, kind="config", doc="integral action time")
    D = Param(float, units="min", precision=2, get=lambda s: s.drv.pid("D"), set=lambda s, v: s.drv.set_pid("D", v),
              poll=10.0, kind="config", doc="derivative action time")
    AutoPID = Param(bool, get=lambda s: s.drv.auto_pid(), set=lambda s, v: s.drv.set_auto_pid(v), poll=10.0,
                    kind="config", doc="PID from the auto-PID table (legacy L / scpi PIDT)")
    # protected: the heater then regulates on another sensor - one that is not on the stage lets it overheat
    HeaterSensor = Param(int, limits=(1, 3), get=lambda s: s.drv.heater_sensor(),
                         set=lambda s, v: s.drv.set_heater_sensor(v), poll=10.0, kind="config", safety="protected",
                         doc="sensor the heater loop uses (legacy only)")
    HeaterManual = Param(float, units="%", limits=(0.0, 99.9), set=lambda s, v: s.drv.set_heater_manual(v),
                         refresh=["Heater"], doc="heater output in heater-manual mode")
    GasManual = Param(float, units="%", limits=(0.0, 99.9), set=lambda s, v: s.drv.set_gas_manual(v),
                      refresh=["GasFlow"], doc="gas flow in gas-manual mode")
    # protected: more heater power can reach the sample
    HeaterMaxV = Param(float, units="V", limits=(0.0, 40.0), get=lambda s: s.drv.heater_max(),
                       set=lambda s, v: s.drv.set_heater_max(v), poll=10.0, kind="config", safety="protected",
                       doc="heater voltage limit (legacy $M, write-only - TransportMeasure sends 40; scpi HTR:VLIM)")
    RampTarget = Param(float, units="K", precision=3, writable=True, kind="config", safety="safe",
                       doc="ramp end temperature")
    RampTime = Param(float, units="min", precision=2, writable=True, initial=10.0, limits=(0.0, 1339.9),
                     kind="config", safety="safe", doc="ramp duration")
    RampHold = Param(float, units="min", precision=2, writable=True, initial=0.0, limits=(0.0, 1339.9),
                     kind="config", safety="safe", doc="hold time at RampTarget (legacy sweep table only)")
    AutoHeater = Param(bool, writable=True, initial=1, kind="config", safety="safe",
                       doc="1: Setpoint/RampStart switch the heater to auto (gas mode kept); 0: never touch modes")
    RampStart = Action("ramp_start", doc="ramp the setpoint to RampTarget over RampTime", safety="confirm",
                       refresh=["Ramping", "Setpoint", "HeaterGasMode"])
    # safe: stopping only holds the present setpoint
    RampStop = Action(lambda s: s.drv.ramp_stop(), safety="safe", doc="stop the ramp, hold the present setpoint",
                      refresh=["Ramping", "Setpoint"])

    ophyd = {"stop": "RampStop"}

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--resource", help="VISA address, e.g. GPIB0::24::INSTR")
        parser.add_argument("--protocol", choices=("legacy", "scpi"), default="legacy",
                            help="legacy = ITC503 command set (as TransportMeasure), scpi = Mercury native")
        parser.add_argument("--sensor", action="append", default=[], dest="sensors",
                            help="scpi: temperature board UIDs, first = control loop (default MB1.T1; repeatable)")
        parser.add_argument("--heater", default="", help="scpi: heater board UID (e.g. MB0.H1) for volts/power/limit")
        parser.add_argument("--aux", default="", help="scpi: auxiliary (gas valve) board UID, e.g. DB4.G1")
        parser.add_argument("--pressure", default="", help="scpi: pressure board UID, e.g. DB5.P1")
        parser.add_argument("--tolerance", type=float, default=0.2, help="setpoint reached within this (K)")
        parser.add_argument("--settle-s", type=float, default=60.0, help="... held this long (s)")
        parser.add_argument("--timeout-s", type=float, default=7200.0, help="set() gives up after this (s)")

    def instance_schema(self) -> dict:
        schema = self.schema()
        for p in schema["params"]:
            if p["name"] == "Setpoint":
                p["positioner"] = {**p["positioner"], "tolerance": self.args.tolerance,
                                   "settle_s": self.args.settle_s, "timeout_s": self.args.timeout_s}
        return schema

    def open(self) -> None:
        a = self.args
        if self.mock:
            io = SimMercuryIO()
        else:
            if not a.resource:
                raise ValueError("--resource is required (or --mock)")
            import pyvisa

            io = pyvisa.ResourceManager().open_resource(a.resource)
            io.read_termination = io.write_termination = "\r" if a.protocol == "legacy" else "\n"
            io.timeout = 3500
        self.drv = Legacy(io) if a.protocol == "legacy" else Scpi(io, a.sensors or ["MB1.T1"], a.heater, a.aux,
                                                                  a.pressure)

    def close(self) -> None:
        close = getattr(getattr(self.drv, "io", None), "close", None)
        if callable(close):
            close()

    def _heater_auto(self) -> None:
        if not self.values["AutoHeater"]:
            return
        mode = self.drv.mode()
        target = "AUTO" if mode in ("MA", "AUTO") else "AM"  # keep the gas mode
        if mode != target:
            self.drv.set_mode(target)

    def write_setpoint(self, value: float) -> None:
        self.drv.set_setpoint(value)
        self._heater_auto()

    def ramp_start(self) -> None:
        self.drv.ramp_start(float(self.values["RampTarget"]), float(self.values["RampTime"]),
                            float(self.values["RampHold"]))
        self._heater_auto()


if __name__ == "__main__":
    run_instrument(MercuryITC)
