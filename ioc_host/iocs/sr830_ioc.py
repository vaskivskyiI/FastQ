"""IOC: Stanford Research SR830 DSP lock-in amplifier (GPIB / RS-232 via VISA).

    python ioc_host/iocs/sr830_ioc.py --resource GPIB0::8::INSTR --prefix FASTQ:SR830:
    python ioc_host/iocs/sr830_ioc.py --mock --prefix TEST:SR830:

X, Y, R, Theta come from ONE `SNAP? 1,2,3,4` per poll (simultaneous sample, as
TransportMeasure TempSweepSystem/devices/lockin.py reads them); the other params map
1:1 onto SR830 commands (FREQ, SLVL, OFLT, SENS, OFSL, SYNC, FMOD, RSLP, PHAS, HARM, ISRC,
ICPL, IGND, ILIN, RMOD, OEXP, OAUX, AUXV). AutoGain / AutoPhase / AutoReserve / AutoOffset*
wait until the SR830 reports the command finished (serial poll bit 1).
Status: LIAS? (latched, cleared by reading) once per second -> InputOverload,
FilterOverload, OutputOverload, RefUnlock.
Buffered acquisition (Acquire): the internal data buffer records X (CH1) and Y (CH2) at
BufferRate for BufferPoints points (DDEF 1,0,0 / DDEF 2,0,0 - the front-panel displays
show X and Y afterwards; SRAT, SEND 0, REST, STRT, SPTS?, PAUS, TRCA?) -> BufferX/BufferY.
With BufferRate "Trigger" a point is stored per TTL edge at the rear TRIG IN.
Commands: SR830 manual (Stanford Research Systems, "Remote programming" chapter;
https://www.thinksrs.com/downloads/pdfs/manuals/SR830m.pdf) and PyMeasure srs/sr830.py.
Safety: sine output amplitude and aux outputs "protected"; reference, input and offset
settings "confirm"; time constant, sensitivity, slope and buffer parameters "safe".
No derived quantities here (resistance etc. -> control-host derived values).
The SR830 answers on the interface OUTX selects; nothing is written at startup, so
if it answers nothing over GPIB, start once with --outx (sends OUTX 1 on connect).
"""
from __future__ import annotations

import math
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

# OFLT / SENS index -> value (more than the 16 states an EPICS enum can hold: published as numbers)
TIME_CONSTANTS = tuple(float(f"{m}e{e}") for e in range(-5, 5) for m in (1, 3))   # 10 us .. 30 ks
SENSITIVITIES = tuple(float(f"{m}e{e}") for e in range(-9, 0) for m in (2, 5, 10))  # 2 nV .. 1 V
SLOPES = ("6dB", "12dB", "18dB", "24dB")
REF_SOURCES = ("External", "Internal")
INPUTS = ("A", "A-B", "I 1MOhm", "I 100MOhm")
COUPLINGS = ("AC", "DC")
GROUNDS = ("Float", "Ground")
LINE_FILTERS = ("None", "Line", "2xLine", "Both")
RESERVES = ("High reserve", "Normal", "Low noise")
REF_TRIGGERS = ("Sine", "TTL rising", "TTL falling")
EXPANDS = ("x1", "x10", "x100")
BUFFER_RATES = tuple(f"{0.0625 * 2 ** i:g} Hz" for i in range(14)) + ("Trigger",)  # SRAT 0..13 (62.5 mHz..512 Hz), 14
BUFFER_MAX = 16383
LIAS_BITS = {"InputOverload": 0, "FilterOverload": 1, "OutputOverload": 2, "RefUnlock": 3}


class Driver:
    def __init__(self, io) -> None:
        self.io = io
        self.snap = (math.nan,) * 4

    def q(self, cmd: str) -> str:
        return self.io.query(cmd).strip()

    def qf(self, cmd: str) -> float:
        return float(self.q(cmd))

    def qi(self, cmd: str) -> int:
        return int(float(self.q(cmd)))

    def w(self, cmd: str) -> None:
        self.io.write(cmd)

    def read_snap(self) -> float:
        self.snap = tuple(float(x) for x in self.q("SNAP? 1,2,3,4").split(","))
        return self.snap[0]

    def run_auto(self, cmd: str, timeout_s: float) -> None:
        """AGAN/APHS/ARSV take seconds; *STB? bit 1 = no command in progress."""
        self.w(cmd)
        t0 = time.monotonic()
        time.sleep(0.2)
        while not int(self.q("*STB? 1")):
            if time.monotonic() - t0 > timeout_s:
                raise TimeoutError(f"{cmd} not finished after {timeout_s:.0f} s")
            time.sleep(0.2)

    def offset_expand(self, ch: int) -> tuple[float, int]:
        """OEXP? i -> (offset %, expand index) of X (1), Y (2), R (3)."""
        off, exp = self.q(f"OEXP? {ch}").split(",")
        return float(off), int(float(exp))

    def set_offset(self, ch: int, offset: float | None = None, expand: int | None = None) -> None:
        old_off, old_exp = self.offset_expand(ch)  # OEXP sets both: keep the other one
        off = old_off if offset is None else offset
        exp = old_exp if expand is None else expand
        self.w(f"OEXP {ch},{off:.2f},{exp}")

    def close(self):
        self.io.close()


class SimSR830IO:
    """Signal = 1 kOhm sample at the sine output through a 1 MOhm series resistor (current 1 nA/mV)."""

    def __init__(self) -> None:
        self.s = {"FREQ": 123.456, "SLVL": 0.1, "OFLT": 8, "SENS": 17, "OFSL": 1, "FMOD": 1, "PHAS": 0.0,
                  "HARM": 1, "ISRC": 0, "ICPL": 0, "IGND": 1, "ILIN": 0, "RMOD": 1, "RSLP": 0, "SYNC": 0, "SRAT": 4,
                  "SEND": 1}
        self.aux, self.busy_until = [0.0] * 4, 0.0
        self.oexp = {1: [0.0, 0], 2: [0.0, 0], 3: [0.0, 0]}
        self.phase0 = 12.0
        self.buf: list[tuple[float, float]] = []
        self.buf_since: float | None = None

    def _xy(self):
        r = self.s["SLVL"] * 1e-3 * (1 + random.gauss(0, 1e-3))
        th = math.radians(self.phase0 - self.s["PHAS"])
        return r * math.cos(th), r * math.sin(th)

    def _outputs(self):
        """X, Y, R with offset/expand applied as the SR830 does: (value - offset% of full scale) * expand."""
        x, y = self._xy()
        fs = SENSITIVITIES[self.s["SENS"]]
        out = []
        for ch, v in ((1, x), (2, y), (3, math.hypot(x, y))):
            off, exp = self.oexp[ch]
            out.append((v - off / 100 * fs) * (1, 10, 100)[exp])
        return out

    def _fill(self):
        if self.buf_since is None or self.s["SRAT"] == 14:
            return
        n = min(BUFFER_MAX, int((time.monotonic() - self.buf_since) * 0.0625 * 2 ** self.s["SRAT"]))
        while len(self.buf) < n:
            self.buf.append(tuple(self._outputs()[:2]))

    def write(self, cmd: str) -> None:
        head, _, arg = cmd.strip().partition(" ")
        if head == "STRT":
            self.buf_since = self.buf_since or time.monotonic()
        elif head == "PAUS":
            self._fill()
            self.buf_since = None
        elif head == "REST":
            self.buf, self.buf_since = [], None
        elif head == "TRIG":
            if self.buf_since is not None and self.s["SRAT"] == 14:
                self.buf.append(tuple(self._outputs()[:2]))
        elif head == "OEXP":
            i, off, exp = arg.split(",")
            self.oexp[int(i)] = [float(off), int(exp)]
        elif head == "AOFF":
            i = int(arg)
            x, y = self._xy()
            off = (x, y, math.hypot(x, y))[i - 1] / SENSITIVITIES[self.s["SENS"]] * 100
            self.oexp[i][0] = max(-105.0, min(105.0, off))  # the SR830 offsets span +-105 %
        elif head == "DDEF":
            pass
        elif head == "AGAN":
            r = math.hypot(*self._xy())
            self.s["SENS"] = next((i for i, v in enumerate(SENSITIVITIES) if v >= 1.5 * r), len(SENSITIVITIES) - 1)
            self.busy_until = time.monotonic() + 1.0
        elif head == "APHS":
            self.s["PHAS"], self.busy_until = self.phase0, time.monotonic() + 0.5
        elif head == "ARSV":
            self.busy_until = time.monotonic() + 0.5
        elif head == "AUXV":
            i, v = arg.split(",")
            self.aux[int(i) - 1] = float(v)
        elif head in self.s:
            self.s[head] = type(self.s[head])(float(arg))

    def query(self, cmd: str) -> str:
        head, _, arg = cmd.strip().partition(" ")
        if head == "*IDN?":
            return "Stanford_Research_Systems,SR830,s/n00000,ver1.07"
        if head == "*STB?":
            return "0" if time.monotonic() < self.busy_until else "1"
        if head == "SNAP?":
            x, y, r = self._outputs()
            x0, y0 = self._xy()
            return f"{x:.6e},{y:.6e},{r:.6e},{math.degrees(math.atan2(y0, x0)):.3f}"
        if head == "OEXP?":
            off, exp = self.oexp[int(arg)]
            return f"{off:.2f},{exp}"
        if head == "LIAS?":
            return str(int(math.hypot(*self._xy()) > SENSITIVITIES[self.s["SENS"]]) << 2)
        if head == "SPTS?":
            self._fill()
            return str(len(self.buf))
        if head == "TRCA?":
            i, j, k = (int(v) for v in arg.split(","))
            self._fill()
            return ",".join(f"{p[i - 1]:.6e}" for p in self.buf[j:j + k]) + ","
        if head == "OAUX?":
            return f"{0.001 * int(arg):.4f}"
        if head == "AUXV?":
            return f"{self.aux[int(arg) - 1]:.3f}"
        return str(self.s[head[:-1]])


def _nearest(values: tuple, v: float) -> int:
    """index of the value nearest to v on a log scale"""
    return min(range(len(values)), key=lambda i: abs(math.log(values[i] / v)))


def _choice(cmd: str, choices: tuple) -> dict:
    """get/set of an enum param that is an SR830 integer command, e.g. OFLT."""
    return dict(get=lambda s: s.drv.qi(cmd + "?"), set=lambda s, v: s.drv.w(f"{cmd} {choices.index(v)}"))


class SR830(Instrument):
    TYPE = "sr830"
    DESCRIPTION = "Stanford Research SR830 DSP lock-in amplifier"
    DEFAULT_PREFIX = "FASTQ:SR830:"
    POLL = 0.5

    X = Param(float, units="V", precision=9, get=lambda s: s.drv.read_snap(), kind="hinted", overview=True,
              trend=True, doc="in-phase output (A for current input)")
    Y = Param(float, units="V", precision=9, get=lambda s: s.drv.snap[1], kind="hinted", overview=True,
              doc="quadrature output")
    R = Param(float, units="V", precision=9, get=lambda s: s.drv.snap[2], overview=True, doc="magnitude")
    Theta = Param(float, units="deg", precision=3, get=lambda s: s.drv.snap[3], overview=True, doc="phase")
    Frequency = Param(float, units="Hz", precision=4, limits=(0.001, 102000.0), poll=2.0,
                      get=lambda s: s.drv.qf("FREQ?"), set=lambda s, v: s.drv.w(f"FREQ {v:.5g}"), kind="config", safety="confirm",
                      overview=True, doc="reference frequency (settable with the internal reference)")
    Amplitude = Param(float, units="V", precision=3, limits=(0.004, 5.0), poll=5.0, get=lambda s: s.drv.qf("SLVL?"),
                      set=lambda s, v: s.drv.w(f"SLVL {v:.3f}"), kind="config", safety="protected",
                      doc="sine output amplitude (rms) - the excitation")
    TimeConstant = Param(float, units="s", precision=6, limits=(1e-5, 3e4), poll=5.0, kind="config", safety="safe",
                         get=lambda s: TIME_CONSTANTS[s.drv.qi("OFLT?")],
                         set=lambda s, v: s.drv.w(f"OFLT {_nearest(TIME_CONSTANTS, v)}"),
                         doc="time constant; a put picks the nearest of 10us, 30us, ... 30ks")
    Sensitivity = Param(float, units="V", precision=9, limits=(2e-9, 1.0), poll=2.0, kind="config", overview=True,
                        safety="safe",
                        get=lambda s: SENSITIVITIES[s.drv.qi("SENS?")],
                        set=lambda s, v: s.drv.w(f"SENS {_nearest(SENSITIVITIES, v)}"),
                        doc="full scale (2nV..1V; x1e-6 = A for current input); a put picks the nearest range")
    FilterSlope = Param("enum", choices=SLOPES, poll=5.0, kind="config", doc="low-pass filter slope", safety="safe",
                        **_choice("OFSL", SLOPES))
    RefSource = Param("enum", choices=REF_SOURCES, poll=5.0, kind="config", doc="reference source", safety="confirm",
                      **_choice("FMOD", REF_SOURCES))
    Phase = Param(float, units="deg", precision=3, limits=(-360.0, 729.99), poll=5.0, get=lambda s: s.drv.qf("PHAS?"),
                  set=lambda s, v: s.drv.w(f"PHAS {v:.3f}"), kind="config", safety="confirm",
                  doc="reference phase shift")
    Harmonic = Param(int, limits=(1, 19999), poll=5.0, get=lambda s: s.drv.qi("HARM?"),
                     set=lambda s, v: s.drv.w(f"HARM {v}"), kind="config", safety="confirm",
                     doc="detection harmonic")
    InputConfig = Param("enum", choices=INPUTS, poll=10.0, kind="config", doc="input (voltage A / A-B / current)",
                        safety="confirm",
                        **_choice("ISRC", INPUTS))
    Coupling = Param("enum", choices=COUPLINGS, poll=10.0, kind="config", safety="confirm", doc="input coupling",
                     **_choice("ICPL", COUPLINGS))
    Grounding = Param("enum", choices=GROUNDS, poll=10.0, kind="config", doc="input shield", safety="confirm",
                      **_choice("IGND", GROUNDS))
    LineFilter = Param("enum", choices=LINE_FILTERS, poll=10.0, kind="config", doc="notch filters", safety="confirm",
                       **_choice("ILIN", LINE_FILTERS))
    Reserve = Param("enum", choices=RESERVES, poll=10.0, kind="config", doc="dynamic reserve", safety="confirm",
                    **_choice("RMOD", RESERVES))
    for _i in (1, 2, 3, 4):
        locals()[f"AuxIn{_i}"] = Param(float, units="V", precision=4, poll=5.0,
                                       get=lambda s, i=_i: s.drv.qf(f"OAUX? {i}"), doc=f"aux input {_i}")
        locals()[f"AuxOut{_i}"] = Param(float, units="V", precision=3, limits=(-10.5, 10.5), poll=10.0,
                                        get=lambda s, i=_i: s.drv.qf(f"AUXV? {i}"),
                                        set=lambda s, v, i=_i: s.drv.w(f"AUXV {i},{v:.3f}"), kind="config",
                                        safety="protected", doc=f"aux output {_i} (may drive another instrument)")
    RefTrigger = Param("enum", choices=REF_TRIGGERS, poll=10.0, kind="config", safety="confirm",
                       doc="external reference input: sine zero crossing / TTL edge", **_choice("RSLP", REF_TRIGGERS))
    SyncFilter = Param(bool, poll=10.0, kind="config", safety="safe", get=lambda s: s.drv.qi("SYNC?"),
                       set=lambda s, v: s.drv.w(f"SYNC {int(v)}"), doc="synchronous filter (below 200 Hz)")
    for _i, _c in ((1, "X"), (2, "Y"), (3, "R")):
        locals()[f"{_c}Offset"] = Param(float, units="%", precision=2, limits=(-105.0, 105.0), poll=10.0,
                                        kind="config", get=lambda s, i=_i: s.drv.offset_expand(i)[0],
                                        set=lambda s, v, i=_i: s.drv.set_offset(i, offset=v), safety="confirm",
                                        doc=f"{_c} output offset (% of full scale)")
        locals()[f"{_c}Expand"] = Param("enum", choices=EXPANDS, poll=10.0, kind="config",
                                        get=lambda s, i=_i: s.drv.offset_expand(i)[1],
                                        set=lambda s, v, i=_i: s.drv.set_offset(i, expand=EXPANDS.index(v)),
                                        safety="confirm", doc=f"{_c} output expand")
    del _c
    # LIAS? is latched and cleared by the read: one read per poll round (the first of these params)
    InputOverload = Param(bool, poll=1.0, get=lambda s: s.lias("InputOverload"), overview=True,
                          doc="input / amplifier overload since the last poll")
    FilterOverload = Param(bool, poll=1.0, get=lambda s: s.lias("FilterOverload"), doc="time constant filter overload")
    OutputOverload = Param(bool, poll=1.0, get=lambda s: s.lias("OutputOverload"), doc="output overload (> full scale)")
    RefUnlock = Param(bool, poll=1.0, get=lambda s: s.lias("RefUnlock"), doc="reference unlocked")
    # buffered acquisition
    BufferRate = Param("enum", choices=BUFFER_RATES, initial="64 Hz", writable=True, kind="config", safety="safe",
                       doc="buffer sample rate (Trigger = one point per TTL edge on TRIG IN)")
    BufferPoints = Param(int, initial=1000, writable=True, limits=(1, BUFFER_MAX), kind="config", safety="safe",
                         doc="points per Acquire")
    BufferTimeoutS = Param(float, units="s", precision=1, initial=0.0, writable=True, limits=(0.0, 1e5),
                           kind="config", safety="safe", doc="Acquire timeout (0 = 2 x points / rate + 10 s)")
    BufferX = Param("array", nelm=BUFFER_MAX, units="V", precision=9, writable=False, initial=[0.0],
                    doc="X of the last Acquire")
    BufferY = Param("array", nelm=BUFFER_MAX, units="V", precision=9, writable=False, initial=[0.0],
                    doc="Y of the last Acquire")
    BufferN = Param(int, writable=False, doc="points of the last Acquire")
    BufferMeanX = Param(float, units="V", precision=9, writable=False, doc="mean X of the last Acquire")
    BufferMeanY = Param(float, units="V", precision=9, writable=False, doc="mean Y of the last Acquire")
    BufferState = Param(str, writable=False, initial="Idle", doc="Idle / Running / Done / Aborted / Error: ...")
    del _i
    AutoGain = Action(lambda s: s.drv.run_auto("AGAN", 60.0), doc="auto gain (sets Sensitivity)", safety="confirm",
                      refresh=["Sensitivity", "X", "Y", "R", "Theta"], timeout_s=60.0)
    AutoPhase = Action(lambda s: s.drv.run_auto("APHS", 30.0), doc="auto phase (Theta -> 0)", safety="confirm",
                       refresh=["Phase", "X", "Y", "R", "Theta"], timeout_s=30.0)
    AutoReserve = Action(lambda s: s.drv.run_auto("ARSV", 60.0), doc="auto reserve", refresh=["Reserve"],
                         safety="confirm", timeout_s=60.0)
    AutoOffsetX = Action(lambda s: s.drv.run_auto("AOFF 1", 30.0), doc="auto offset X (X -> 0)", safety="confirm",
                         refresh=["XOffset", "X"], timeout_s=30.0)
    AutoOffsetY = Action(lambda s: s.drv.run_auto("AOFF 2", 30.0), doc="auto offset Y", safety="confirm",
                         refresh=["YOffset", "Y"], timeout_s=30.0)
    AutoOffsetR = Action(lambda s: s.drv.run_auto("AOFF 3", 30.0), doc="auto offset R", safety="confirm",
                         refresh=["ROffset", "R"], timeout_s=30.0)
    Acquire = Action("acquire", exclusive=True, safety="safe", refresh=[], timeout_s=40000.0,
                     doc="buffered acquisition of X and Y (BufferRate, BufferPoints); completes with the data")
    Abort = Action("abort", lock=False, safety="safe", doc="end a running Acquire (keeps the points so far)")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--resource", help="VISA address, e.g. GPIB0::8::INSTR")
        parser.add_argument("--outx", action="store_true", help="send OUTX 1 (replies to GPIB) on connect")

    def open(self) -> None:
        if self.mock:
            self.drv = Driver(SimSR830IO())
            return
        if not self.args.resource:
            raise ValueError("--resource is required (or --mock)")
        import pyvisa

        io = pyvisa.ResourceManager().open_resource(self.args.resource)
        io.read_termination, io.write_termination, io.timeout = "\n", "\n", 10000  # TRCA? of a full buffer
        self.drv = Driver(io)
        if self.args.outx:
            self.drv.w("OUTX 1")
        self.drv.q("*IDN?")

    def __init__(self, args) -> None:
        super().__init__(args)
        self._abort = threading.Event()
        self._lias = 0

    def lias(self, name: str) -> bool:
        if name == "InputOverload":  # first of the group: one LIAS? per poll round
            self._lias = self.drv.qi("LIAS?")
        return bool(self._lias >> LIAS_BITS[name] & 1)

    def acquire(self) -> None:
        self._abort.clear()
        d, v = self.drv, self.values
        n, rate = int(v["BufferPoints"]), BUFFER_RATES.index(v["BufferRate"])
        self.publish(BufferState="Running", BufferN=0)
        try:
            for cmd in ("DDEF 1,0,0", "DDEF 2,0,0", f"SRAT {rate}", "SEND 0", "REST", "STRT"):
                d.w(cmd)
            timeout = float(v["BufferTimeoutS"]) or (math.inf if rate == 14 else 2 * n / (0.0625 * 2 ** rate) + 10)
            t0 = time.monotonic()
            while (done := d.qi("SPTS?")) < n:
                if self._abort.wait(0.25):
                    break
                if time.monotonic() - t0 > timeout:
                    raise TimeoutError(f"{done}/{n} buffer points after {timeout:.0f} s")
            d.w("PAUS")
            done = min(d.qi("SPTS?"), n)
            x = [float(t) for t in d.q(f"TRCA? 1,0,{done}").split(",") if t.strip()] if done else []
            y = [float(t) for t in d.q(f"TRCA? 2,0,{done}").split(",") if t.strip()] if done else []
            self.publish(BufferX=x or [0.0], BufferY=y or [0.0],
                         BufferMeanX=sum(x) / len(x) if x else math.nan,
                         BufferMeanY=sum(y) / len(y) if y else math.nan, BufferN=len(x))
            self.publish(BufferState=f"Aborted ({len(x)} points)" if self._abort.is_set() else "Done")
        except Exception as exc:
            try:
                d.w("PAUS")
            except Exception:
                pass
            self.publish(BufferState=f"Error: {type(exc).__name__}: {exc}"[:200])
            raise

    def abort(self) -> None:
        self._abort.set()


if __name__ == "__main__":
    run_instrument(SR830)
