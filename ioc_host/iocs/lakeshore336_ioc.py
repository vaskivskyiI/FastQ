"""IOC: Lake Shore Model 336 temperature controller (VISA: GPIB / USB / Ethernet, plain SCPI-style commands).

    python ioc_host/iocs/lakeshore336_ioc.py --resource GPIB0::12::INSTR --prefix FASTQ:LS336:
    python ioc_host/iocs/lakeshore336_ioc.py --mock --tolerance 0.1 --settle-s 2 --prefix TEST:LS336:

Inputs A-D: temperature (KRDG?), sensor units (SRDG?), reading status (RDGST?),
name (INNAME?), sensor type (INTYPE?), alarm (ALARM / ALARMST? / ALMRST).
Heater outputs 1 and 2 (the two PID loops with heaters): setpoint (SETP), range
(RANGE), output % (HTR?), heater status (HTRST?), heater setup (HTRSET), PID,
ramp (RAMP / RAMPST?), manual output (MOUT), output mode + control input
(OUTMODE), autotune (ATUNE / TUNEST?). Relays 1-2 (RELAYST?). Commands from the
Model 336 manual (chapter 6) and the lakeshore Python driver (model_336.py,
temperature_controllers.py). Not implemented: outputs 3/4 (analog/warm-up),
zones (ZONE), curves (CRV*), INTYPE writes (sensor setup belongs on the front panel).
ControlTemp<n> follows OUTMODE. The loop chosen with --loop (default 2: TransportMeasure
drives loop/output 2 with input A) also has the interface every temperature controller has
(control/fastq/roles.py): Temp, Setpoint (the positioner `mv(dev, T)` moves), Heater, Ramping,
RampTarget / RampTime / RampStart / RampStop (a ramp = RAMP rate |target - setpoint| / time).
Startup only reads.
"""
from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

INPUTS = "ABCD"
RANGES = ("Off", "Low", "Medium", "High")
OUTMODES = ("Off", "Closed loop", "Zone", "Open loop", "Monitor out", "Warmup supply")
CONTROL_INPUTS = ("None", "A", "B", "C", "D")
SENSOR_TYPES = ("Disabled", "Diode", "Platinum RTD", "NTC RTD", "Thermocouple", "Capacitance")
HEATER_STATUS = ("OK", "Open load", "Short")
HTR_RES = ("?", "25 Ohm", "50 Ohm")                         # HTRSET resistance 1, 2
HTR_MAXI = ("User", "0.707 A", "1 A", "1.141 A", "2 A")     # HTRSET max current 0..4
TUNE_MODES = ("P", "PI", "PID")
RDG_BITS = ((1, "invalid"), (16, "T under"), (32, "T over"), (64, "units zero"), (128, "units over"))


class Driver:
    def __init__(self, io) -> None:
        self.io = io
        self._outmode: dict[int, tuple[float, tuple]] = {}

    def ask(self, cmd: str) -> list[str]:
        return [x.strip() for x in self.io.query(cmd).strip().split(",")]

    def q(self, cmd: str) -> list[float]:
        return [float(x) for x in self.ask(cmd)]

    def w(self, cmd: str) -> None:
        self.io.write(cmd)

    def temp(self, ch: str) -> float:
        return self.q(f"KRDG? {ch}")[0]

    def reading_status(self, ch: str) -> str:
        bits = int(self.q(f"RDGST? {ch}")[0])
        return ", ".join(name for b, name in RDG_BITS if bits & b) or "OK"

    def outmode(self, loop: int) -> tuple[int, int, int]:
        """(mode, input 0..4, powerup enable), cached 5 s: ControlTemp reads it every poll."""
        t, v = self._outmode.get(loop, (0.0, None))
        if v is None or time.monotonic() - t > 5.0:
            v = tuple(int(x) for x in self.q(f"OUTMODE? {loop}")[:3])
            self._outmode[loop] = (time.monotonic(), v)
        return v

    def set_outmode(self, loop: int, mode=None, inp=None) -> None:
        self._outmode.pop(loop, None)
        cur_mode, cur_inp, powerup = self.outmode(loop)
        mode = cur_mode if mode is None else mode
        inp = cur_inp if inp is None else inp
        self._outmode.pop(loop, None)
        self.w(f"OUTMODE {loop},{mode},{inp},{powerup}")

    def control_temp(self, loop: int):
        inp = self.outmode(loop)[1]
        return self.temp(INPUTS[inp - 1]) if 1 <= inp <= 4 else None

    def pid(self, loop: int, i: int) -> float:
        return self.q(f"PID? {loop}")[i]

    def set_pid(self, loop: int, i: int, value: float) -> None:
        pid = self.q(f"PID? {loop}")[:3]
        pid[i] = value
        self.w(f"PID {loop},{pid[0]:g},{pid[1]:g},{pid[2]:g}")

    def ramp(self, loop: int) -> tuple[int, float]:
        on, rate = self.q(f"RAMP? {loop}")[:2]
        return int(on), rate

    def set_ramp(self, loop: int, on=None, rate=None) -> None:
        cur_on, cur_rate = self.ramp(loop)
        on = cur_on if on is None else int(bool(on))
        self.w(f"RAMP {loop},{on},{cur_rate if rate is None else rate:g}")

    def htrset(self, loop: int) -> list[float]:
        """resistance (1/2), max current index (0..4), max user current (A), display (1 current / 2 power)."""
        return self.q(f"HTRSET? {loop}")[:4]

    def set_htrset(self, loop: int, res=None, maxi=None) -> None:
        r, m, user, disp = self.htrset(loop)
        r = r if res is None else res
        m = m if maxi is None else maxi
        self.w(f"HTRSET {loop},{int(r)},{int(m)},{user:g},{int(disp)}")

    def alarm(self, ch: str) -> list[float]:
        """enable, high, low, deadband, latch, audible, visible."""
        return self.q(f"ALARM? {ch}")[:7]

    def set_alarm(self, ch: str, index: int, value: float) -> None:
        a = self.alarm(ch)
        a[index] = value
        self.w(f"ALARM {ch},{int(a[0])},{a[1]:g},{a[2]:g},{a[3]:g},{int(a[4])},{int(a[5])},{int(a[6])}")

    def alarm_state(self, ch: str) -> str:
        hi, lo = (int(x) for x in self.q(f"ALARMST? {ch}")[:2])
        return "High" if hi else "Low" if lo else "None"

    def tune_status(self) -> str:
        active, out, err, stage = self.ask("TUNEST?")[:4]
        if int(err):
            return f"error (output {out}, stage {stage})"
        return f"tuning output {out}, stage {stage}" if int(active) else "idle"

    def close(self):
        self.io.close()


class SimLS336IO:
    """Inputs follow the loop setpoints (first order, tau_s) when the heater range is on."""

    def __init__(self, start_k: float = 20.0, tau_s: float = 3.0) -> None:
        self.t = {c: start_k + 0.1 * i for i, c in enumerate(INPUTS)}
        self.sp = {1: start_k, 2: start_k}
        self.rng, self.outm = {1: 0, 2: 3}, {1: [1, 2, 0], 2: [1, 1, 0]}
        self.pid = {1: [50.0, 20.0, 0.0], 2: [35.0, 20.0, 0.0]}
        self.rampc, self.mout, self.tau = {1: [0, 1.0], 2: [0, 1.0]}, {1: 0.0, 2: 0.0}, tau_s
        self.htrset = {1: [1, 2, 0.0, 1], 2: [2, 1, 0.0, 1]}
        self.alarms = {c: [0, 300.0, 0.0, 1.0, 0, 0, 1] for c in INPUTS}
        self.latched = {c: [0, 0] for c in INPUTS}
        self.names = {c: f"Input {c}" for c in INPUTS}
        self.tune = None  # (loop, t_end)
        self.ramp_sp = dict(self.sp)
        self._last = time.monotonic()

    def _step(self):
        now = time.monotonic()
        dt, self._last = now - self._last, now
        for loop in (1, 2):
            on, rate = self.rampc[loop]
            tgt = self.sp[loop]
            if on:  # setpoint ramps at rate K/min, 60x faster than real
                d = rate * dt
                self.ramp_sp[loop] += max(-d, min(d, tgt - self.ramp_sp[loop]))
            else:
                self.ramp_sp[loop] = tgt
            mode, inp = self.outm[loop][:2]
            if self.rng[loop] and 1 <= inp <= 4:
                c = INPUTS[inp - 1]
                goal = self.ramp_sp[loop] if mode == 1 else 4.0 + 3.0 * self.mout[loop] * self.rng[loop]
                self.t[c] += (goal - self.t[c]) * (1 - math.exp(-dt / self.tau))
        for c in INPUTS:
            en, hi, lo = self.alarms[c][:3]
            state = [int(en and self.t[c] > hi), int(en and self.t[c] < lo)]
            latch = self.alarms[c][4]
            self.latched[c] = [a or b for a, b in zip(self.latched[c], state)] if latch else state
        if self.tune and now > self.tune[1]:
            self.tune = None

    def _htr(self, loop):
        if not self.rng[loop]:
            return 0.0
        if self.outm[loop][0] == 3:
            return self.mout[loop]
        c = INPUTS[self.outm[loop][1] - 1]
        return max(0.0, min(100.0, 20.0 + 10.0 * (self.ramp_sp[loop] - self.t[c])))

    def write(self, cmd: str) -> None:
        self._step()
        head, _, args = cmd.strip().partition(" ")
        a = args.split(",")
        if head == "ALMRST":
            self.latched = {c: [0, 0] for c in INPUTS}
            return
        if head == "ALARM":
            self.alarms[a[0]] = [float(x) for x in a[1:8]]
            return
        loop = int(a[0]) if a[0] else 0
        if head == "SETP":
            self.sp[loop] = float(a[1])
        elif head == "RANGE":
            self.rng[loop] = int(a[1])
        elif head == "PID":
            self.pid[loop] = [float(x) for x in a[1:4]]
        elif head == "RAMP":
            self.rampc[loop] = [int(a[1]), float(a[2])]
            inp = self.outm[loop][1]
            self.ramp_sp[loop] = self.t[INPUTS[inp - 1]] if 1 <= inp <= 4 else self.sp[loop]
        elif head == "MOUT":
            self.mout[loop] = float(a[1])
        elif head == "OUTMODE":
            self.outm[loop] = [int(x) for x in a[1:4]]
        elif head == "HTRSET":
            self.htrset[loop] = [int(a[1]), int(a[2]), float(a[3]), int(a[4])]
        elif head == "ATUNE":
            self.tune = (loop, time.monotonic() + 20.0)
        else:
            raise ValueError(f"simulator: unknown command {cmd!r}")

    def query(self, cmd: str) -> str:
        self._step()
        head, _, arg = cmd.strip().partition(" ")
        if head == "*IDN?":
            return "LSCI,MODEL336,SIMULATED,1.0"
        if head == "KRDG?":
            return f"+{self.t[arg] + random.gauss(0, 2e-4):08.4f}"
        if head == "SRDG?":
            return f"+{100.0 + 0.4 * self.t[arg]:08.3f}"  # a Pt100-like resistance
        if head == "RDGST?":
            return "000"
        if head == "INNAME?":
            return self.names[arg]
        if head == "INTYPE?":
            return "2,1,0,1,1"
        if head == "ALARM?":
            return ",".join(f"{x:g}" for x in self.alarms[arg])
        if head == "ALARMST?":
            return ",".join(map(str, self.latched[arg]))
        if head == "TUNEST?":
            return f"{int(bool(self.tune))},{self.tune[0] if self.tune else 0},0,{5 if self.tune else 0}"
        if head == "RELAYST?":
            return "0"
        loop = int(arg) if arg else 0
        return {"SETP?": lambda: f"+{self.ramp_sp[loop]:.4f}", "RANGE?": lambda: str(self.rng[loop]),
                "HTR?": lambda: f"+{self._htr(loop):.2f}", "OUTMODE?": lambda: ",".join(map(str, self.outm[loop])),
                "PID?": lambda: ",".join(f"+{x:.1f}" for x in self.pid[loop]),
                "RAMP?": lambda: f"{self.rampc[loop][0]},+{self.rampc[loop][1]:.3f}",
                "RAMPST?": lambda: str(int(self.rampc[loop][0] and abs(self.ramp_sp[loop] - self.sp[loop]) > 1e-6)),
                "MOUT?": lambda: f"+{self.mout[loop]:.2f}", "HTRST?": lambda: "0",
                "HTRSET?": lambda: "{},{},{:+.3f},{}".format(*self.htrset[loop])}[head]()

    def close(self):
        pass


def _input_params(c: str) -> dict:
    """The params of input c (A-D)."""
    d = lambda s: s.drv  # noqa: E731
    main = c in "AB"
    return {
        f"Temp{c}": Param(float, units="K", precision=4, get=lambda s: d(s).temp(c), poll=None if main else 2.0,
                          kind="hinted" if c == "A" else "normal", overview=main, trend=c == "A", doc=f"input {c}"),
        f"Sensor{c}": Param(float, precision=4, poll=5.0, get=lambda s: d(s).q(f"SRDG? {c}")[0],
                            doc=f"input {c} in sensor units (V / Ohm / mV / pF)"),
        f"Status{c}": Param(str, poll=5.0, get=lambda s: d(s).reading_status(c), overview=main,
                            doc=f"input {c} reading status (RDGST?: OK, invalid, T under/over, units zero/over)"),
        f"Name{c}": Param(str, poll=0, get=lambda s: d(s).ask(f"INNAME? {c}")[0], kind="config",
                          doc=f"input {c} name (front panel)"),
        f"SensorType{c}": Param("enum", choices=SENSOR_TYPES, poll=0, kind="config",
                                get=lambda s: int(d(s).q(f"INTYPE? {c}")[0]), doc=f"input {c} sensor type (INTYPE?)"),
        f"AlarmState{c}": Param("enum", choices=("None", "High", "Low"), poll=5.0, get=lambda s: d(s).alarm_state(c),
                                doc=f"input {c} alarm (latched alarms stay until AlarmReset)"),
        # alarms are protected: they can drive the relays, which may be wired as interlocks
        f"AlarmEnable{c}": Param(bool, poll=0, get=lambda s: d(s).alarm(c)[0], kind="config", safety="protected",
                                 set=lambda s, v: d(s).set_alarm(c, 0, int(v)), refresh=[f"AlarmState{c}"],
                                 doc=f"input {c} alarm on/off"),
        f"AlarmHigh{c}": Param(float, units="K", precision=3, poll=0, get=lambda s: d(s).alarm(c)[1], kind="config",
                               set=lambda s, v: d(s).set_alarm(c, 1, v), safety="protected",
                               refresh=[f"AlarmState{c}"], doc=f"input {c} high alarm threshold (input units)"),
        f"AlarmLow{c}": Param(float, units="K", precision=3, poll=0, get=lambda s: d(s).alarm(c)[2], kind="config",
                              set=lambda s, v: d(s).set_alarm(c, 2, v), safety="protected",
                              refresh=[f"AlarmState{c}"], doc=f"input {c} low alarm threshold (input units)"),
    }


def _loop_params(n: int) -> dict:
    """The params of heater loop n (1 or 2)."""
    d = lambda s: s.drv  # noqa: E731
    return {
        f"ControlTemp{n}": Param(float, units="K", precision=4, get=lambda s: d(s).control_temp(n),
                                 kind="hinted" if n == 2 else "normal", trend=n == 2,
                                 doc=f"temperature of the input loop {n} controls (OUTMODE)"),
        f"Setpoint{n}": Param(float, units="K", precision=4, limits=(0.0, 1500.0), poll=2.0,
                              get=lambda s: d(s).q(f"SETP? {n}")[0], set=lambda s, v: d(s).w(f"SETP {n},{v:g}"),
                              doc=f"loop {n} setpoint (ramps at RampRate{n} if RampEnable{n})"),
        # protected: the range sets the heater power that can reach the sample
        f"HeaterRange{n}": Param("enum", choices=RANGES, poll=2.0, get=lambda s: int(d(s).q(f"RANGE? {n}")[0]),
                                 set=lambda s, v: d(s).w(f"RANGE {n},{RANGES.index(v)}"), overview=True,
                                 kind="config", safety="protected", doc=f"heater {n} range (Off stops heating)"),
        f"Heater{n}": Param(float, units="%", precision=1, get=lambda s: d(s).q(f"HTR? {n}")[0], overview=True,
                            doc=f"heater {n} output"),
        f"HeaterStatus{n}": Param("enum", choices=HEATER_STATUS, poll=5.0, get=lambda s: int(d(s).q(f"HTRST? {n}")[0]),
                                  overview=True, doc=f"heater {n} error (HTRST?; open load / short switch it off)"),
        f"HeaterResistance{n}": Param("enum", choices=HTR_RES, poll=0, get=lambda s: int(d(s).htrset(n)[0]),
                                      set=lambda s, v: d(s).set_htrset(n, res=HTR_RES.index(v)), kind="config",
                                      safety="protected", doc=f"heater {n} resistance setting (HTRSET)"),
        f"HeaterMaxCurrent{n}": Param("enum", choices=HTR_MAXI, poll=0, get=lambda s: int(d(s).htrset(n)[1]),
                                      set=lambda s, v: d(s).set_htrset(n, maxi=HTR_MAXI.index(v)), kind="config",
                                      safety="protected", doc=f"heater {n} max current (HTRSET; User = front panel)"),
        f"ManualOutput{n}": Param(float, units="%", precision=2, limits=(0.0, 100.0), poll=10.0,
                                  get=lambda s: d(s).q(f"MOUT? {n}")[0], set=lambda s, v: d(s).w(f"MOUT {n},{v:g}"),
                                  kind="config", doc=f"output {n} manual (open loop) value"),
        f"P{n}": Param(float, precision=1, limits=(0.1, 1000.0), poll=10.0, get=lambda s: d(s).pid(n, 0),
                       set=lambda s, v: d(s).set_pid(n, 0, v), kind="config", doc=f"loop {n} gain"),
        f"I{n}": Param(float, precision=1, limits=(0.1, 1000.0), poll=10.0, get=lambda s: d(s).pid(n, 1),
                       set=lambda s, v: d(s).set_pid(n, 1, v), kind="config", doc=f"loop {n} reset"),
        f"D{n}": Param(float, units="%", precision=1, limits=(0.0, 200.0), poll=10.0, get=lambda s: d(s).pid(n, 2),
                       set=lambda s, v: d(s).set_pid(n, 2, v), kind="config", doc=f"loop {n} rate"),
        f"RampEnable{n}": Param(bool, poll=5.0, get=lambda s: d(s).ramp(n)[0],
                                set=lambda s, v: d(s).set_ramp(n, on=v), kind="config",
                                doc=f"setpoint {n} changes ramp at RampRate{n}"),
        f"RampRate{n}": Param(float, units="K/min", precision=3, limits=(0.1, 100.0), poll=5.0,
                              get=lambda s: d(s).ramp(n)[1], set=lambda s, v: d(s).set_ramp(n, rate=v),
                              kind="config", doc=f"loop {n} setpoint ramp rate (0.1-100 K/min)"),
        f"Ramping{n}": Param(bool, poll=2.0, get=lambda s: int(d(s).q(f"RAMPST? {n}")[0]),
                             doc=f"loop {n} setpoint is ramping"),
        # protected: Open loop / Warmup heat without a temperature limit; a wrong input heats blindly
        f"OutputMode{n}": Param("enum", choices=OUTMODES, poll=10.0, get=lambda s: d(s).outmode(n)[0],
                                set=lambda s, v: d(s).set_outmode(n, mode=OUTMODES.index(v)), kind="config",
                                safety="protected", refresh=[f"ControlInput{n}"], doc=f"output {n} mode (OUTMODE)"),
        f"ControlInput{n}": Param("enum", choices=CONTROL_INPUTS, poll=10.0, get=lambda s: d(s).outmode(n)[1],
                                  set=lambda s, v: d(s).set_outmode(n, inp=CONTROL_INPUTS.index(v)), kind="config",
                                  safety="protected", refresh=[f"ControlTemp{n}"], doc=f"input loop {n} controls"),
        f"Autotune{n}": Action(lambda s, mode: s.drv.w(f"ATUNE {n},{TUNE_MODES.index(mode)}"), arg=str,
                               refresh=["TuneStatus"], doc=f"autotune loop {n}: put P, PI or PID (ATUNE); "
                                                           "the controller then steps the heater for minutes"),
    }


class LakeShore336(Instrument):
    TYPE = "lakeshore336"
    DESCRIPTION = "Lake Shore 336 temperature controller (inputs A-D, heater loops 1 and 2)"
    DEFAULT_PREFIX = "FASTQ:LS336:"
    POLL = 1.0
    ophyd = {"positioner": "Setpoint", "stop": "RampStop"}

    # ---- the temperature-controller interface, for the loop of --loop
    Temp = Param(float, units="K", precision=4, get=lambda s: s.drv.control_temp(s.args.loop), kind="hinted",
                 overview=True, trend=True, doc="control temperature of the --loop loop")
    Setpoint = Param(float, units="K", precision=4, limits=(0.0, 1500.0), poll=2.0, overview=True,
                     get=lambda s: s.drv.q(f"SETP? {s.args.loop}")[0],
                     set=lambda s, v: s.drv.w(f"SETP {s.args.loop},{v:g}"), refresh=["Ramping"],
                     positioner={"readback": "Temp", "tolerance": 0.1, "settle_s": 60.0, "timeout_s": 7200.0},
                     doc="setpoint of the --loop loop; set() completes when Temp is within tolerance for settle_s")
    Heater = Param(float, units="%", precision=1, get=lambda s: s.drv.q(f"HTR? {s.args.loop}")[0],
                   doc="heater output of the --loop loop")
    Ramping = Param(bool, poll=2.0, get=lambda s: int(s.drv.q(f"RAMPST? {s.args.loop}")[0]),
                    doc="the --loop setpoint is ramping")
    RampTarget = Param(float, units="K", precision=3, writable=True, kind="config", safety="safe",
                       doc="ramp end temperature")
    RampTime = Param(float, units="min", precision=2, writable=True, initial=10.0, limits=(0.01, 10000.0),
                     kind="config", safety="safe", doc="ramp duration (rate = |target - setpoint| / time)")
    RampStart = Action("ramp_start", refresh=["Ramping", "Setpoint"],
                       doc="ramp the --loop setpoint to RampTarget over RampTime (rate 0.1-100 K/min)")
    RampStop = Action("ramp_stop", safety="safe", refresh=["Ramping", "Setpoint"],
                      doc="stop the ramp, hold the present setpoint")

    for _c in INPUTS:
        locals().update(_input_params(_c))
    locals().update(_loop_params(1))
    locals().update(_loop_params(2))
    del _c
    TuneStatus = Param(str, poll=5.0, get=lambda s: s.drv.tune_status(), doc="autotune state (TUNEST?)")
    Relay1 = Param(bool, poll=5.0, get=lambda s: int(s.drv.q("RELAYST? 1")[0]), kind="config", doc="relay 1 energised")
    Relay2 = Param(bool, poll=5.0, get=lambda s: int(s.drv.q("RELAYST? 2")[0]), kind="config", doc="relay 2 energised")
    AlarmReset = Action(lambda s: s.drv.w("ALMRST"), refresh=[f"AlarmState{c}" for c in INPUTS],
                        doc="clear latched alarms (ALMRST)")
    # safe: switching the heaters off is the safe direction (it disturbs a measurement, but harms nothing)
    HeatersOff = Action(lambda s: (s.drv.w("RANGE 1,0"), s.drv.w("RANGE 2,0")), safety="safe",
                        refresh=["HeaterRange1", "HeaterRange2", "Heater1", "Heater2"],
                        doc="heater range Off on outputs 1 and 2")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--resource", help="VISA address, e.g. GPIB0::12::INSTR")
        parser.add_argument("--loop", type=int, choices=(1, 2), default=2, help="loop moved by mv(dev, T)")
        parser.add_argument("--tolerance", type=float, default=0.1, help="setpoint reached within this (K)")
        parser.add_argument("--settle-s", type=float, default=60.0, help="... held this long (s)")
        parser.add_argument("--timeout-s", type=float, default=7200.0, help="set() gives up after this (s)")

    def instance_schema(self) -> dict:
        schema = self.schema()
        for p in schema["params"]:
            if p["name"] == "Setpoint":
                p["positioner"] = {**p["positioner"], "tolerance": self.args.tolerance,
                                   "settle_s": self.args.settle_s, "timeout_s": self.args.timeout_s}
        return schema

    def ramp_start(self) -> None:
        n = self.args.loop
        sp = float(self.drv.q(f"SETP? {n}")[0])
        target = float(self.values["RampTarget"])
        rate = min(100.0, max(0.1, abs(target - sp) / max(float(self.values["RampTime"]), 1e-3)))
        self.drv.set_ramp(n, on=1, rate=round(rate, 3))
        self.drv.w(f"SETP {n},{target:g}")

    def ramp_stop(self) -> None:
        n = self.args.loop
        here = float(self.drv.q(f"SETP? {n}")[0])  # the ramping setpoint now
        self.drv.set_ramp(n, on=0)
        self.drv.w(f"SETP {n},{here:g}")

    def open(self) -> None:
        if self.mock:
            self.drv = Driver(SimLS336IO())
            return
        if not self.args.resource:
            raise ValueError("--resource is required (or --mock)")
        import pyvisa

        io = pyvisa.ResourceManager().open_resource(self.args.resource)
        io.read_termination = io.write_termination = "\n"
        io.timeout = 3500
        self.drv = Driver(io)


if __name__ == "__main__":
    run_instrument(LakeShore336)
