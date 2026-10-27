"""IOC: Xeryon piezo stage controller (serial 115200 baud; protocol of 4dmag
fdm/Xeryon.py, Xeryon library v1.88). One IOC per controller, one or more axes.

    python ioc_host/iocs/xeryon_ioc.py --port COM7 --axes "X:XLS_312:mm:GasCell" --prefix FDM:GASCELL:
    python ioc_host/iocs/xeryon_ioc.py --port COM8 --axes "X:XLS_1250:mm,Y:XLS_1250:mm,Z:XRTU_30_109:deg" \\
        --prefix FDM:XERYON1:
    python ioc_host/iocs/xeryon_ioc.py --mock --prefix TEST:XERYON:

--axes "Letter:Stage[:unit[:Name]],..." - Stage is a member of the Stage table
below (encoder resolution), unit one of mm um nm deg mrad rad enc, Name the PV
name of the axis (default: the letter). Per axis: <Name> (DPOS set, EPOS
readback, a positioner), <Name>Moving, <Name>Dpos, <Name>Velocity, <Name>LowLimit /
<Name>HighLimit (LLIM / RLIM software limits), <Name>Ptol / <Name>Pto2 (position
tolerances), <Name>Indexed, <Name>ClosedLoop, <Name>MotorOn, <Name>AtLimit,
<Name>Error, <Name>Stat / <Name>Status, <Name>JogStep and the actions <Name>Step
(relative), <Name>JogForward / <Name>JogReverse, <Name>Scan (SCAN=+-1 / 0: continuous
move), <Name>FindIndex / <Name>Stop. Commands and STAT bits as in the vendor's Python
library (Xeryon.py v1.88, 4dmag fdm/Xeryon.py). Not exposed: drive amplitude / phase /
frequency settings (MAMP, PHAC, CFRQ ... - stage-specific, go through --settings + Init),
SAVE (writes the controller's flash), trigger outputs (TRGS/TRGW/TRGP/TRGN).

The controller streams EPOS/DPOS/STAT lines by itself; they are collected on
every read (no background thread). Startup only listens. 4dmag's start()
(reset, stage + settings upload, ENBL=1) is the Init action; Enable (ENBL=1)
clears thermal / error-limit / safety-timeout stops.
"""
from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

DEFAULT_AXES = "X:XLS_312:mm"


def rot(cmd: str, counts_per_rev: int) -> tuple:
    return False, cmd, 2e6 * math.pi / counts_per_rev, 100


# stage: (linear, encoder resolution command, resolution in nm or urad, speed multiplier) - 4dmag Xeryon.Stage
STAGES = {
    "XLS_312": (True, "XLS1=312", 312.5, 1000), "XLS_1250": (True, "XLS1=1251", 1250, 1000),
    "XLS_1250_OLD": (True, "XLS1=1250", 1250, 1000), "XLS_1250_OLD_2": (True, "XLS1=1250", 312.5, 1000),
    "XLS_78": (True, "XLS1=78", 78.125, 1000), "XLS_5": (True, "XLS1=5", 5, 1000), "XLS_1": (True, "XLS1=1", 1, 1000),
    "XLS_312_3N": (True, "XLS3=312", 312.5, 1000), "XLS_1250_3N": (True, "XLS3=1251", 1250, 1000),
    "XLS_1250_3N_OLD": (True, "XLS3=1250", 312.5, 1000), "XLS_78_3N": (True, "XLS3=78", 78.125, 1000),
    "XLS_5_3N": (True, "XLS3=5", 5, 1000), "XLS_1_3N": (True, "XLS3=1", 1, 1000),
    "XLA_312": (True, "XLA1=312", 312.5, 1000), "XLA_1250": (True, "XLA1=1250", 1250, 1000),
    "XLA_78": (True, "XLA1=78", 78.125, 1000), "XLA_OL": (True, "XLA1=0", 1, 1000),
    "XLA_OL_3N": (True, "XLA3=0", 1, 1000),
    "XLA_312_3N": (True, "XLA3=312", 312.5, 1000), "XLA_1250_3N": (True, "XLA3=1250", 1250, 1000),
    "XLA_78_3N": (True, "XLA3=78", 78.125, 1000), "XLA_312_OLD": (True, "XLA=312", 312.5, 1000),
    "XLA_1250_OLD": (True, "XLA=1250", 1250, 1000), "XLA_78_OLD": (True, "XLA=78", 78.125, 1000),
    "XRTA": rot("XRTA=109", 57600),
    "XRTU_40_3": rot("XRT1=2", 2764800), "XRTU_40_19": rot("XRT1=18", 345600),
    "XRTU_40_49": rot("XRT1=47", 135000), "XRTU_40_109": rot("XRT1=73", 86400),
    "XRTU_30_3": rot("XRT1=3", 1843200), "XRTU_30_19": rot("XRT1=19", 360000),
    "XRTU_30_49": rot("XRT1=49", 144000), "XRTU_30_109": rot("XRT1=109", 57600),
    "XRTU_60_3": rot("XRT3=3", 2073600), "XRTU_60_19": rot("XRT3=19", 324000),
    "XRTU_60_49": rot("XRT3=49", 129600), "XRTU_60_109": rot("XRT3=109", 64800),
    "XRTU_30_109_OLD": rot("XRTU=109", 57600), "XRTU_40_73_OLD": rot("XRTU=73", 86400),
    "XRTU_40_3_OLD": rot("XRTU=3", 1800000),
}
# base units (nm linear, urad rotary - the unit of the stage resolution) per user unit
UNITS = {"mm": 1e6, "um": 1e3, "nm": 1.0, "deg": 2e6 * math.pi / 360, "mrad": 1e3, "rad": 1e6}
STAT_BITS = {2: "thermal protection 1", 3: "thermal protection 2", 12: "encoder error", 14: "at left end",
             15: "at right end", 16: "error limit", 18: "safety timeout", 21: "position fail"}
STOP_BITS = (2, 3, 14, 15, 16, 18, 21)  # a pending move ended here without reaching the target
AMPLITUDE_MULTIPLIER, PHASE_MULTIPLIER = 1456.0, 182


class Axis:
    def __init__(self, letter: str, stage: str, unit: str, name: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}; one of {', '.join(STAGES)}")
        self.letter, self.stage, self.name = letter, stage, name
        self.linear, self.res_cmd, self.res, self.speed_mult = STAGES[stage]
        self.unit = unit or ("mm" if self.linear else "deg")
        if self.unit != "enc" and (self.unit in ("mm", "um", "nm")) != self.linear:
            raise ValueError(f"axis {letter}: unit {self.unit} does not fit stage {stage}")
        self.pending, self.t_cmd = False, 0.0  # a DPOS / index search was commanded and has not finished
        self.dpos_valid = False  # DPOS is the reference of a step (not after index search / scan / reset)

    def to_enc(self, value: float, unit: str | None = None) -> int:
        unit = unit or self.unit
        return round(value) if unit == "enc" else round(float(value) * UNITS[unit] / self.res)

    def from_enc(self, enc: float) -> float:
        return float(enc) if self.unit == "enc" else float(enc) * self.res / UNITS[self.unit]


class Driver:
    """Line protocol: 'L:TAG=value\\n' (no 'L:' on a single-axis system); the
    controller sends 'L:TAG=value' lines (EPOS, DPOS, STAT, TIME ... at POLI, and answers to TAG=?)."""

    def __init__(self, port: str, baud: int, letters: list[str]) -> None:
        import serial

        self.ser = serial.Serial(port, baud, timeout=0.01)
        self.ser.reset_input_buffer()
        self.single = len(letters) <= 1
        self.letters = letters
        self.data: dict[str, dict] = {L: {} for L in letters}
        self.stamp: dict[tuple, float] = {}
        self.buf = b""

    def send(self, letter: str | None, cmd: str) -> None:
        prefix = "" if self.single or letter is None else letter + ":"
        self.ser.write((prefix + cmd + "\n").encode())

    def pump(self) -> None:
        n = self.ser.in_waiting
        if n:
            self.buf += self.ser.read(n)
        *lines, self.buf = self.buf.split(b"\n")
        now = time.monotonic()
        for raw in lines:
            line = raw.decode(errors="replace").strip()
            if "=" not in line:
                continue
            letter = self.letters[0]
            if line.count(":") == 1:
                head, line = line.split(":")
                letter = head if head in self.data else letter
            tag, _, val = line.partition("=")
            try:
                self.data[letter][tag.strip()] = int(val.replace(" ", ""))
            except ValueError:
                continue
            self.stamp[letter, tag.strip()] = now

    def get(self, letter: str, tag: str, max_age: float = 1.0, wait: float = 0.5) -> int:
        """latest streamed value; asks with TAG=? when the stream did not bring it recently."""
        self.pump()
        if time.monotonic() - self.stamp.get((letter, tag), -1e9) > max_age:
            t0 = time.monotonic()
            self.send(letter, tag + "=?")
            while self.stamp.get((letter, tag), -1e9) < t0:
                if time.monotonic() - t0 > wait:
                    raise TimeoutError(f"Xeryon: no {letter}:{tag}")
                time.sleep(0.01)
                self.pump()
        return self.data[letter][tag]

    def close(self):
        self.ser.close()


class SimDriver:
    """Controller answers for --mock: closed-loop stage moving at SSPD."""

    def __init__(self, port: str, baud: int, letters: list[str]) -> None:
        self.ax = {L: {"EPOS": 0.0, "DPOS": 0, "STAT": 1 << 5 | 1 << 6, "SSPD": 10000, "PTO2": 10, "PTOL": 2,
                       "LLIM": -40000, "RLIM": 40000, "SCAN": 0, "t": time.monotonic(), "index": 0.0}
                   for L in letters}
        self.single = len(letters) <= 1
        self.letters = letters

    def _update(self, a):
        now = time.monotonic()
        dt, a["t"] = now - a["t"], now
        if a["index"]:
            if now > a["index"]:
                a["index"], a["STAT"] = 0.0, (a["STAT"] | 1 << 8) & ~(1 << 9)
            return
        step = max(a["SSPD"], 1) * 20 * dt  # encoder counts; fast enough for tests
        if a["SCAN"]:  # continuous move until a software limit
            a["DPOS"] = a["RLIM"] if a["SCAN"] > 0 else a["LLIM"]
        d = a["DPOS"] - a["EPOS"]
        a["EPOS"] = a["DPOS"] if abs(d) <= step else a["EPOS"] + math.copysign(step, d)
        reached = abs(a["DPOS"] - a["EPOS"]) <= a["PTO2"]
        at_l, at_r = a["EPOS"] <= a["LLIM"], a["EPOS"] >= a["RLIM"]
        if (at_l and a["SCAN"] < 0) or (at_r and a["SCAN"] > 0):
            a["SCAN"] = 0
        st = a["STAT"] & ~(1 << 10 | 1 << 13 | 1 << 14 | 1 << 15)
        a["STAT"] = st | reached << 10 | bool(a["SCAN"]) << 13 | at_l << 14 | at_r << 15

    def send(self, letter, cmd):
        a = self.ax[letter or self.letters[0]]
        self._update(a)
        tag, _, val = cmd.partition("=")
        if val == "?":
            return
        if tag == "DPOS":
            a["DPOS"] = min(max(int(val), a["LLIM"]), a["RLIM"])  # the controller clips to its limits
        elif tag == "SCAN":
            a["SCAN"] = int(val)
            if not a["SCAN"]:
                a["DPOS"] = int(a["EPOS"])
        elif tag == "STOP":
            a["DPOS"], a["SCAN"] = int(a["EPOS"]), 0
        elif tag == "INDX":
            a["index"], a["STAT"] = time.monotonic() + 0.5, a["STAT"] | 1 << 9
            a["EPOS"] = a["DPOS"] = 0
        elif tag == "RSET":
            a["STAT"] &= ~(1 << 8)
        elif val.lstrip("-").isdigit():
            a[tag] = int(val)

    def get(self, letter, tag, max_age=1.0, wait=0.5):
        a = self.ax[letter]
        self._update(a)
        return int(round(a[tag] + (random.gauss(0, 0.3) if tag == "EPOS" else 0)))

    def close(self):
        pass


def parse_axes(text: str) -> list[Axis]:
    axes = []
    for item in filter(None, (s.strip() for s in text.split(","))):
        letter, stage, *rest = item.split(":") + ["", ""]
        axes.append(Axis(letter, stage, rest[0], rest[1] or letter))
    if not axes:
        raise ValueError("--axes: at least one axis")
    return axes


def settings_lines(path: str, axes: list[Axis]) -> list[tuple[Axis | None, str, str]]:
    """4dmag readSettings(): 'TAG=v', 'L:TAG=v', '%' comments; multi-axis lines without a letter
    are master settings. Values are converted like applySettingMultipliers()."""
    out = []
    by_letter = {a.letter: a for a in axes}
    for line in Path(path).read_text().splitlines():
        line = line.split("%")[0].replace(" ", "").strip()
        if "=" not in line:
            continue
        axis = axes[0]
        if ":" in line:
            letter, line = line.split(":", 1)
            if letter not in by_letter:
                continue
            axis = by_letter[letter]
        elif len(axes) > 1:
            out.append((None, *line.split("=", 1)))
            continue
        tag, value = line.split("=", 1)
        if tag in ("MAMP", "MIMP", "OFSA", "OFSB", "AMPL", "MAM2"):
            value = str(int(int(value) * AMPLITUDE_MULTIPLIER))
        elif tag in ("PHAC", "PHAS"):
            value = str(int(int(value) * PHASE_MULTIPLIER))
        elif tag in ("SSPD", "MSPD", "ISPD"):
            value = str(int(float(value) * axis.speed_mult))
        elif tag in ("LLIM", "RLIM", "HLIM", "ZON1", "ZON2"):
            value = str(axis.to_enc(float(value), "mm" if axis.linear else "deg"))
        elif tag == "MASS":
            m = int(value)
            tag, value = "CFRQ", str(next(c for lim, c in ((50, 100000), (100, 60000), (250, 30000), (500, 10000),
                                                             (1000, 5000), (1e99, 3000)) if m <= lim))
        out.append((axis, tag, value))
    return out


class Xeryon(Instrument):
    TYPE = "xeryon"
    DESCRIPTION = "Xeryon piezo stage controller (closed-loop linear / rotation stages)"
    DEFAULT_PREFIX = "FDM:XERYON1:"
    POLL = 1.0
    AXES: list = []
    ophyd = {"stop": "Stop"}

    Stop = Action("stop_all", safety="safe", doc="stop all axes")
    Enable = Action(lambda s: [s.drv.send(a.letter, "ENBL=1") for a in s.AXES], safety="confirm",
                    doc="ENBL=1 on every axis: resume after a thermal / error-limit / safety-timeout stop")
    Init = Action("init", safety="confirm", timeout_s=30.0,
                  doc="4dmag start(): reset all axes (index lost!), send stage type and --settings file, ENBL=1")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--port", default="COM7")
        parser.add_argument("--baud", type=int, default=115200)
        parser.add_argument("--axes", default=DEFAULT_AXES, help='"Letter:Stage[:unit[:Name]],..."')
        parser.add_argument("--settings", default="", help="settings_default.txt sent by the Init action")

    def __init__(self, args) -> None:
        super().__init__(args)
        self.AXES = parse_axes(args.axes)  # own Axis objects (pending-move state) per instance

    def open(self) -> None:
        cls = SimDriver if self.mock else Driver
        self.drv = cls(self.args.port, self.args.baud, [a.letter for a in self.AXES])

    def stat(self, a: Axis) -> int:
        return self.drv.get(a.letter, "STAT")

    def bit(self, stat: int, n: int) -> bool:
        return bool(stat >> n & 1)

    def move(self, a: Axis, value: float) -> None:
        self.drv.send(a.letter, f"DPOS={a.to_enc(value)}")
        a.pending, a.t_cmd, a.dpos_valid = True, time.monotonic(), True

    def moving(self, a: Axis) -> bool:
        st = self.stat(a)
        if self.bit(st, 9) or self.bit(st, 13):  # searching index / scanning
            return True
        if a.pending and time.monotonic() - a.t_cmd > 0.3:  # STAT lags the command by a few updates
            a.pending = not (self.bit(st, 10) or any(self.bit(st, b) for b in STOP_BITS))
        return a.pending

    def status_text(self, a: Axis) -> str:
        st = self.stat(a)
        flags = [txt for b, txt in STAT_BITS.items() if self.bit(st, b)]
        if not self.bit(st, 8):
            flags.append("index not found")
        if self.bit(st, 9):
            flags.append("searching index")
        return ", ".join(flags) or ("position reached" if self.bit(st, 10) else "ok")

    def speed(self, a: Axis) -> float:
        """SSPD: um/s (linear) or 0.01 deg/s (rotary) -> units/s."""
        raw = self.drv.get(a.letter, "SSPD", max_age=0.0)
        return a.from_enc(a.to_enc(raw, "um")) if a.linear else a.from_enc(a.to_enc(raw / 100, "deg"))

    def set_speed(self, a: Axis, value: float) -> None:
        enc = a.to_enc(value)
        raw = round(a.res * enc / UNITS["um"]) if a.linear else round(a.res * enc / UNITS["deg"] * 100)
        self.drv.send(a.letter, f"SSPD={int(raw)}")

    def step(self, a: Axis, delta: float) -> None:
        """Xeryon library step(): relative to DPOS (or EPOS after an index search / scan)."""
        base = self.drv.get(a.letter, "DPOS" if a.dpos_valid else "EPOS")
        self.drv.send(a.letter, f"DPOS={int(base) + a.to_enc(delta)}")
        a.pending, a.t_cmd, a.dpos_valid = True, time.monotonic(), True

    def scan(self, a: Axis, direction: int) -> None:
        """SCAN=+1/-1: closed-loop continuous move at SSPD until SCAN=0, STOP or a software limit."""
        self.drv.send(a.letter, f"SCAN={(direction > 0) - (direction < 0)}")
        a.pending, a.dpos_valid = False, False

    def set_enc(self, a: Axis, tag: str, value: float) -> None:
        self.drv.send(a.letter, f"{tag}={a.to_enc(value)}")

    def find_index(self, a: Axis) -> None:
        self.drv.send(a.letter, "INDX=0")
        a.pending, a.t_cmd, a.dpos_valid = True, time.monotonic(), False

    def stop(self, a: Axis) -> None:
        self.drv.send(a.letter, "STOP=0")
        a.pending = False

    def stop_all(self) -> None:
        for a in self.AXES:
            self.stop(a)

    def init(self) -> None:
        for a in self.AXES:
            self.drv.send(a.letter, "RSET=0")
            a.pending = a.dpos_valid = False
        time.sleep(0.2)
        lines = settings_lines(self.args.settings, self.AXES) if self.args.settings else []
        for axis, tag, value in lines:
            if axis is None:
                self.drv.send(None, f"{tag}={value}")
        for a in self.AXES:
            self.drv.send(a.letter, a.res_cmd)
            for axis, tag, value in lines:
                if axis is a:
                    self.drv.send(a.letter, f"{tag}={value}")
        for a in self.AXES:
            self.drv.send(a.letter, "ENBL=1")
            if a.stage == "XRTA":
                self.drv.send(a.letter, "ENBL=3")


def make_class(axes: list[Axis]) -> type[Xeryon]:
    """Xeryon with one set of params per axis (from --axes)."""
    attrs: dict = {}
    for i, ax in enumerate(axes):
        n = ax.name

        def A(s, i=i):  # this instance's Axis object
            return s.AXES[i]

        attrs[n] = Param(float, units=ax.unit, precision=5, poll=0.2, kind="hinted", overview=True, trend=True,
                         get=lambda s, A=A: A(s).from_enc(s.drv.get(A(s).letter, "EPOS")),
                         set=lambda s, v, A=A: s.move(A(s), v),
                         positioner={"readback": n, "moving": n + "Moving", "tolerance": ax.from_enc(10) * 1.5,
                                     "settle_s": 0.0, "timeout_s": 120.0},
                         doc=f"axis {ax.letter} ({ax.stage}): put = DPOS, readback = EPOS (encoder)")
        attrs[n + "Moving"] = Param(bool, poll=0.2, kind="omitted", get=lambda s, A=A: s.moving(A(s)))
        attrs[n + "Dpos"] = Param(float, units=ax.unit, precision=5, poll=1.0, kind="config",
                                  get=lambda s, A=A: A(s).from_enc(s.drv.get(A(s).letter, "DPOS")),
                                  doc="desired position as the controller has it")
        attrs[n + "Velocity"] = Param(float, units=ax.unit + "/s", precision=3, limits=(0.0, 1e6), poll=0, kind="config",
                                   get=lambda s, A=A: s.speed(A(s)), set=lambda s, v, A=A: s.set_speed(A(s), v),
                                   safety="safe", doc="SSPD (closed-loop speed; also the speed of Scan)")
        # software end stops: the controller does not move beyond them (protects what the stage carries)
        for tag, suffix, side in (("LLIM", "LowLimit", "left"), ("RLIM", "HighLimit", "right")):
            attrs[n + suffix] = Param(float, units=ax.unit, precision=4, poll=0, kind="config",
                                      get=lambda s, A=A, tag=tag: A(s).from_enc(s.drv.get(A(s).letter, tag,
                                                                                          max_age=0.0)),
                                      set=lambda s, v, A=A, tag=tag: s.set_enc(A(s), tag, v),
                                      doc=f"{tag}: {side} software limit (not kept over a power cycle unless "
                                          "saved in the controller / settings file)")
        for tag, doc in (("PTOL", "position tolerance: within it the position counts as reached"),
                         ("PTO2", "second position tolerance (used while settling)")):
            attrs[n + tag.capitalize()] = Param(int, units="enc", limits=(0, 1_000_000), poll=0, kind="config",
                                                get=lambda s, A=A, tag=tag: s.drv.get(A(s).letter, tag, max_age=0.0),
                                                set=lambda s, v, A=A, tag=tag: s.drv.send(A(s).letter, f"{tag}={v}"),
                                                doc=f"{tag}: {doc}, encoder counts (closed-loop setting)")
        attrs[n + "Indexed"] = Param(bool, poll=2.0, kind="config", get=lambda s, A=A: s.bit(s.stat(A(s)), 8),
                                     doc="encoder index found (absolute position valid)")
        attrs[n + "ClosedLoop"] = Param(bool, poll=1.0, kind="config", get=lambda s, A=A: s.bit(s.stat(A(s)), 6),
                                        doc="closed-loop control active (STAT bit 6)")
        attrs[n + "MotorOn"] = Param(bool, poll=1.0, kind="config", get=lambda s, A=A: s.bit(s.stat(A(s)), 5),
                                     doc="motor on (STAT bit 5)")
        attrs[n + "AtLimit"] = Param(bool, poll=1.0, kind="config",
                                     get=lambda s, A=A: s.bit(s.stat(A(s)), 14) or s.bit(s.stat(A(s)), 15),
                                     doc="at the left or right end stop (STAT bits 14/15)")
        attrs[n + "Error"] = Param(bool, poll=1.0, overview=True,
                                   get=lambda s, A=A: any(s.bit(s.stat(A(s)), b) for b in (2, 3, 12, 16, 18, 21)),
                                   doc="thermal protection, encoder error, error limit, safety timeout or "
                                       "position fail - Enable resumes")
        attrs[n + "Stat"] = Param(int, poll=1.0, kind="omitted", get=lambda s, A=A: s.stat(A(s)),
                                  doc="raw STAT word")
        attrs[n + "Status"] = Param(str, poll=1.0, kind="omitted", get=lambda s, A=A: s.status_text(A(s)),
                                    doc="decoded STAT flags")
        attrs[n + "JogStep"] = Param(float, units=ax.unit, precision=4, writable=True, initial=0.1,
                                     limits=(0.0, 1e6), kind="config", safety="safe",
                                     doc=f"step of {n}JogForward / {n}JogReverse")
        attrs[n + "Step"] = Action(lambda s, v, A=A: s.step(A(s), v), arg=float,
                                   doc=f"move by this many {ax.unit} (relative to the desired position)")
        attrs[n + "JogForward"] = Action(lambda s, A=A, n=n: s.step(A(s), s.values[n + "JogStep"]), doc="step +JogStep")
        attrs[n + "JogReverse"] = Action(lambda s, A=A, n=n: s.step(A(s), -s.values[n + "JogStep"]), doc="step -JogStep")
        attrs[n + "Scan"] = Action(lambda s, v, A=A: s.scan(A(s), v), arg=int,
                                   doc="continuous move at Velocity: +1 / -1 until 0, Stop or a software limit")
        attrs[n + "FindIndex"] = Action(lambda s, A=A: s.find_index(A(s)), safety="confirm", timeout_s=60.0,
                                        doc="search the encoder index (moves the stage; Moving = 1 while searching)")
        attrs[n + "Stop"] = Action(lambda s, A=A: s.stop(A(s)), safety="safe", doc="STOP=0",
                                   refresh=[n, n + "Moving"])
    return type("XeryonAxes", (Xeryon,), attrs)


def class_from_argv(argv=None) -> type[Xeryon]:
    """The params depend on --axes, so they are built before run_instrument parses the command line."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--axes", default=DEFAULT_AXES)
    return make_class(parse_axes(pre.parse_known_args(argv)[0].axes))


if __name__ == "__main__":
    run_instrument(class_from_argv())
