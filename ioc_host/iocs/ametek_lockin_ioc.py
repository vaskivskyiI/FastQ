"""IOC: AMETEK / Signal Recovery DSP lock-in amplifier 7270 / 7230 / 7124 (USB, VISA RAW).

    python ioc_host/iocs/ametek_lockin_ioc.py --model 7270 --serial 12345678 --prefix FDM:LOCKIN:
    python ioc_host/iocs/ametek_lockin_ioc.py --resource USB0::0x0A2D::0x001B::12345678::RAW --prefix FDM:LOCKIN:
    python ioc_host/iocs/ametek_lockin_ioc.py --mock --prefix TEST:LOCKIN:

Commands as in 4dmag fdm/lockin_class.py: every command is a query (over USB
the lock-in answers each one; the reply is cut at the first LF, which drops
the status/overload bytes). XY. gives X,Y in V (R and theta are computed from
them), ADCn gives mV, SEN n (1..27: 2 nV .. 1 V full scale, 1-2-5 steps),
TC n (0..30: 10 us .. 100 ks), IE (reference: internal / external rear /
external front), REFP. (phase, deg), FRQ. (reference frequency, Hz),
VMODE (input A / -B / A-B). OF. (internal oscillator frequency) is not in
4dmag - added for the internal reference. Sensitivity and time constant are
floats in V / s (EPICS enums hold only 16 states); a put picks the nearest
setting at or above the value.

Further commands (not used by 4dmag; documented in PyMeasure ametek/ametek7270.py and
signalrecovery/dsp_base.py, frappy_psi SR_7270.py): OA. (oscillator amplitude, V rms),
REFN (harmonic), SLOPE (6/12/18/24 dB/oct), IMODE (voltage / current high bandwidth /
current low noise), CP (AC/DC coupling), FLOAT (input shield), FET (bipolar / FET
preamplifier), ACGAIN (0..90 dB in 10 dB steps), AUTOMATIC (automatic AC gain), NOISEMODE,
DAC. n (rear DAC outputs 1..4, V), SEN. / TC. (sensitivity / time constant as values).
Sensitivity reads SEN. - in the current modes it is in A, as are X/Y/R; a put picks the code
from the ratio SEN. / SEN-code value of the present mode.
Lab model: 7270 (4dmag MagnetPlusPumpProbe: LockIn(<serial>, '7270')).
Safety: oscillator amplitude and DAC outputs "protected"; input / reference settings
"confirm"; sensitivity, time constant, slope, AC gain "safe".
"""
from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ioc_host.common.devkit import Action, Instrument, Param, run_instrument  # noqa: E402

USB_PID = {"7270": "0x001B", "7230": "0x0027", "7124": "0x0018"}
REFERENCE = ("Internal", "External rear", "External front")
VMODE = ("Grounded", "A", "-B", "A-B")
IMODE = ("Voltage", "Current high BW", "Current low noise")
SLOPES = ("6 dB/oct", "12 dB/oct", "18 dB/oct", "24 dB/oct")
COUPLINGS = ("AC", "DC")
SHIELDS = ("Ground", "Float")
FETS = ("Bipolar", "FET")


def code_value(code: int, exp0: int) -> float:
    """1-2-5 sequence of the SEN / TC codes (lockin_class.py sensitivity/time_const_realunits)."""
    return (1, 2, 5)[code % 3] * 10.0 ** (code // 3 + exp0)


def value_code(value: float, codes: range, exp0: int) -> int:
    """Smallest code whose value is >= value (the largest one if none)."""
    return next((c for c in codes if code_value(c, exp0) >= value * (1 - 1e-9)), codes[-1])


SEN_CODES, SEN_EXP = range(1, 28), -9
TC_CODES, TC_EXP = range(0, 31), -5


class Driver:
    def __init__(self, resource: str) -> None:
        import pyvisa

        self.inst = pyvisa.ResourceManager().open_resource(resource)
        self.inst.timeout = 3000

    def query(self, cmd: str) -> str:
        return self.inst.query(cmd).split("\n")[0].strip().strip("\x00")

    def close(self):
        self.inst.close()


class SimDriver:
    def __init__(self) -> None:
        self.s = {"SEN": 22, "TC": 12, "IE": 0, "REFP.": 0.0, "VMODE": 1, "OF.": 1000.0, "OA.": 1.0, "REFN": 1,
                  "SLOPE": 1, "IMODE": 0, "CP": 0, "FLOAT": 0, "FET": 1, "ACGAIN": 2, "AUTOMATIC": 0, "NOISEMODE": 0}
        self.dac = [0.0] * 4

    def query(self, cmd: str) -> str:
        parts = cmd.strip().upper().split()
        key, args = parts[0], parts[1:]
        if key == "DAC.":
            if len(args) == 2:
                self.dac[int(args[0]) - 1] = float(args[1])
                return ""
            return f"{self.dac[int(args[0]) - 1]:.4f}"
        if args:
            self.s[key] = float(args[0]) if "." in key else int(args[0])
            return ""
        mult = (1.0, 1e-6, 1e-8)[self.s["IMODE"]]
        if key == "SEN.":
            return f"{code_value(self.s['SEN'], SEN_EXP) * mult:.4E}"
        if key == "TC.":
            return f"{code_value(self.s['TC'], TC_EXP):.4E}"
        if key == "XY.":
            amp = 1.234e-3 * self.s["OA."] * mult * (1 + 0.002 * random.gauss(0, 1))
            ph = math.radians(30.0 - self.s["REFP."])
            return f"{amp * math.cos(ph):.6E},{amp * math.sin(ph):.6E}"
        if key.startswith("ADC"):
            return str(int(1000 * int(key[3]) + random.gauss(0, 2)))
        if key == "FRQ.":
            return f"{self.s['OF.'] if self.s['IE'] == 0 else 1013.7:.4E}"
        if key == "AQN":
            self.s["REFP."] = 30.0
        if key in ("AS", "AQN", "AXO"):
            return ""
        return str(self.s[key])


class AmetekLockin(Instrument):
    TYPE = "ametek_lockin"
    DESCRIPTION = "AMETEK / Signal Recovery DSP lock-in amplifier (7270 / 7230 / 7124)"
    DEFAULT_PREFIX = "FDM:LOCKIN:"
    POLL = 0.5

    X = Param(float, units="V", precision=6, get=lambda s: s.xy()[0], kind="hinted", overview=True, trend=True,
              doc="in-phase output")
    Y = Param(float, units="V", precision=6, get=lambda s: s.xy()[1], kind="hinted", overview=True,
              doc="quadrature output")
    R = Param(float, units="V", precision=6, get=lambda s: math.hypot(*s.xy()), overview=True, doc="magnitude")
    Theta = Param(float, units="deg", precision=2, get=lambda s: math.degrees(math.atan2(s.xy()[1], s.xy()[0])),
                  doc="phase")
    AuxIn1 = Param(float, units="V", precision=4, get=lambda s: s.adc(1), poll=1.0, doc="auxiliary ADC 1 input")
    AuxIn2 = Param(float, units="V", precision=4, get=lambda s: s.adc(2), poll=1.0)
    AuxIn3 = Param(float, units="V", precision=4, get=lambda s: s.adc(3), poll=1.0)
    AuxIn4 = Param(float, units="V", precision=4, get=lambda s: s.adc(4), poll=1.0)
    Sensitivity = Param(float, units="V", precision=9, limits=(1e-16, 1.0), get=lambda s: float(s.q("SEN.")),
                        set="set_sensitivity", poll=2.0, kind="config", overview=True, safety="safe",
                        doc="full scale (voltage mode 2 nV .. 1 V; current modes in A); a put picks the range >= value")
    TimeConstant = Param(float, units="s", precision=5, limits=(1e-5, 1e5), get=lambda s: float(s.q("TC.")),
                         set=lambda s, v: s.q(f"TC {value_code(v, TC_CODES, TC_EXP)}"), poll=2.0, kind="config",
                         overview=True, safety="safe", doc="output filter time constant (10 us .. 100 ks)")
    FilterSlope = Param("enum", choices=SLOPES, get=lambda s: int(s.q("SLOPE")),
                  set=lambda s, v: s.q(f"SLOPE {SLOPES.index(v)}"), poll=5.0, kind="config", safety="safe",
                  doc="output filter roll-off")
    NoiseMode = Param(bool, get=lambda s: int(s.q("NOISEMODE")), set=lambda s, v: s.q(f"NOISEMODE {int(v)}"),
                      poll=5.0, kind="config", safety="confirm",
                      doc="noise mode (on restricts the time constants; off allows the whole range)")
    RefSource = Param("enum", choices=REFERENCE, get=lambda s: int(s.q("IE")),
                      set=lambda s, v: s.q(f"IE {REFERENCE.index(v)}"), poll=5.0, kind="config", safety="confirm",
                      doc="reference channel")
    Phase = Param(float, units="deg", precision=3, limits=(-360.0, 360.0), get=lambda s: float(s.q("REFP.")),
                     set=lambda s, v: s.q(f"REFP. {v:.3f}"), poll=2.0, kind="config", safety="confirm",
                     doc="reference phase")
    Harmonic = Param(int, limits=(1, 127), get=lambda s: int(s.q("REFN")), set=lambda s, v: s.q(f"REFN {v}"),
                     poll=5.0, kind="config", safety="confirm", doc="detection at this harmonic of the reference")
    RefFrequency = Param(float, units="Hz", precision=3, get=lambda s: float(s.q("FRQ.")), poll=2.0,
                         overview=True, doc="measured reference frequency")
    Frequency = Param(float, units="Hz", precision=3, limits=(0.0, 250e3), get=lambda s: float(s.q("OF.")),
                         set=lambda s, v: s.q(f"OF. {v:.4f}"), poll=5.0, kind="config", safety="confirm",
                         doc="internal oscillator frequency (used with Reference = Internal)")
    Amplitude = Param(float, units="V", precision=4, limits=(0.0, 5.0), get=lambda s: float(s.q("OA.")),
                         set=lambda s, v: s.q(f"OA. {v:.4f}"), poll=2.0, kind="config", safety="protected",
                         overview=True, doc="oscillator output amplitude (RMS) - the excitation")
    InputMode = Param("enum", choices=VMODE, get=lambda s: int(s.q("VMODE")),
                      set=lambda s, v: s.q(f"VMODE {VMODE.index(v)}"), poll=5.0, kind="config", safety="confirm",
                      doc="voltage input: A, -B or A-B (differential)")
    CurrentMode = Param("enum", choices=IMODE, get=lambda s: int(s.q("IMODE")),
                        set=lambda s, v: s.q(f"IMODE {IMODE.index(v)}"), poll=5.0, kind="config", safety="confirm",
                        doc="input: voltage, or current (high bandwidth / low noise) - X/Y/R then in A")
    Coupling = Param("enum", choices=COUPLINGS, get=lambda s: int(s.q("CP")),
                     set=lambda s, v: s.q(f"CP {COUPLINGS.index(v)}"), poll=5.0, kind="config", safety="confirm",
                     doc="input coupling")
    Shield = Param("enum", choices=SHIELDS, get=lambda s: int(s.q("FLOAT")),
                   set=lambda s, v: s.q(f"FLOAT {SHIELDS.index(v)}"), poll=5.0, kind="config", safety="confirm",
                   doc="input connector shields grounded / floating (1 kOhm to ground)")
    Preamp = Param("enum", choices=FETS, get=lambda s: int(s.q("FET")),
                   set=lambda s, v: s.q(f"FET {FETS.index(v)}"), poll=5.0, kind="config", safety="confirm",
                   doc="voltage input device: bipolar (low noise, low source impedance) / FET")
    ACGain = Param(int, units="dB", limits=(0, 90), get=lambda s: 10 * int(s.q("ACGAIN")),
                   set=lambda s, v: s.q(f"ACGAIN {int(v) // 10}"), poll=5.0, kind="config", safety="safe",
                   doc="AC signal gain, 0..90 dB in 10 dB steps")
    AutoACGain = Param(bool, get=lambda s: int(s.q("AUTOMATIC")), set=lambda s, v: s.q(f"AUTOMATIC {int(v)}"),
                       poll=5.0, kind="config", safety="safe", doc="automatic AC gain")
    for _n in (1, 2, 3, 4):
        locals()[f"AuxOut{_n}"] = Param(float, units="V", precision=4, limits=(-10.0, 10.0), poll=5.0, kind="config",
                                     get=lambda s, n=_n: float(s.q(f"DAC. {n}")),
                                     set=lambda s, v, n=_n: s.q(f"DAC. {n} {v:.4f}"), safety="protected",
                                     doc=f"rear DAC output {_n} (may drive another instrument)")
    del _n

    AutoSensitivity = Action(lambda s: s.q("AS"), doc="auto-sensitivity", timeout_s=30, refresh=["Sensitivity"],
                             safety="confirm")
    AutoPhase = Action(lambda s: s.q("AQN"), doc="auto-phase (Y -> 0)", timeout_s=30, refresh=["Phase"],
                       safety="confirm")
    AutoOffset = Action(lambda s: s.q("AXO"), doc="auto-offset (X and Y -> 0)", timeout_s=30, safety="confirm")

    @classmethod
    def add_arguments(cls, parser) -> None:
        parser.add_argument("--model", choices=sorted(USB_PID), default="7270")
        parser.add_argument("--serial", default="", help="USB serial number")
        parser.add_argument("--resource", default="", help="VISA resource (overrides --model/--serial)")

    def open(self) -> None:
        self._xy, self._xy_t = (0.0, 0.0), 0.0
        if self.mock:
            self.drv = SimDriver()
            return
        a = self.args
        if not a.resource and not a.serial:
            raise ValueError("give --serial (and --model) or --resource")
        self.drv = Driver(a.resource or f"USB0::0x0A2D::{USB_PID[a.model]}::{a.serial}::RAW")

    def q(self, cmd: str) -> str:
        return self.drv.query(cmd)

    def xy(self) -> tuple[float, float]:
        """One XY. per poll round: X, Y, R and Theta come from the same sample."""
        if time.monotonic() - self._xy_t > 0.1:
            x, y = self.q("XY.").split(",")
            self._xy, self._xy_t = (float(x), float(y)), time.monotonic()
        return self._xy

    def set_sensitivity(self, value: float) -> None:
        """SEN takes a code; its value depends on IMODE: scale by the present SEN. / code value."""
        unit = float(self.q("SEN.")) / code_value(int(self.q("SEN")), SEN_EXP)
        self.q(f"SEN {value_code(value / unit, SEN_CODES, SEN_EXP)}")

    def adc(self, n: int) -> float:
        return float(self.q(f"ADC{n}")) / 1000.0  # integer form: mV


if __name__ == "__main__":
    run_instrument(AmetekLockin)
